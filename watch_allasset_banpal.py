import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import List, Optional
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup

@dataclass
class DCPost:
    no: int
    title: str
    author: str
    link: str
    created: str
    body: str = ""

def fetch_html(url: str, session: requests.Session, timeout: int = 30) -> str:
    # GitHub Secrets에서 가져온 쿠키를 헤더에 주입
    dc_cookie = os.getenv("DC_COOKIE", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": dc_cookie
    }
    resp = session.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text

def parse_list(html: str, list_url: str, target_nick: str) -> List[DCPost]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select("table.gall_list tbody tr")
    if not rows: rows = soup.select("tbody tr")
    posts: List[DCPost] = []

    for tr in rows:
        no_attr = tr.get("data-no")
        if not no_attr: continue
        try:
            no = int(str(no_attr).strip())
        except ValueError: continue

        writer_td = tr.find("td", class_="gall_writer")
        if not writer_td: continue
        nick = writer_td.get("data-nick", "").strip()
        if not nick:
            nick_el = writer_td.find(class_="nickname")
            nick = nick_el.get_text(strip=True) if nick_el else writer_td.get_text(strip=True)

        if nick.strip() != target_nick: continue

        title_a = tr.select_one("td.gall_tit a")
        if not title_a: continue
        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        link = urljoin(list_url, href) if href else list_url
        date_td = tr.find("td", class_="gall_date")
        created = (date_td.get("title") or date_td.get_text(strip=True)).strip() if date_td else ""

        posts.append(DCPost(no=no, title=title, author=nick, link=link, created=created))
    
    posts.sort(key=lambda p: p.no, reverse=True)
    return posts

def parse_article(html: str, post: DCPost) -> DCPost:
    soup = BeautifulSoup(html, "html.parser")
    title_el = soup.select_one(".title_subject")
    if title_el: post.title = title_el.get_text(strip=True)
    body_el = soup.select_one(".write_div")
    post.body = body_el.get_text("\n", strip=True) if body_el else ""
    return post

def load_state(path: str) -> dict:
    if not os.path.exists(path): return {"last_seen_no": 0, "initialized": False}
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return {"last_seen_no": 0, "initialized": False}

def save_state(path: str, last_seen_no: int, initialized: bool = True) -> None:
    state = {
        "last_seen_no": int(last_seen_no),
        "initialized": bool(initialized),
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def create_issue(repo: str, token: str, alert_to: Optional[str], post: DCPost) -> None:
    api_url = f"https://api.github.com/repos/{repo}/issues"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    issue_title = f"[AllAsset] {post.author} 새 글: {post.title}"
    mention = f"@{alert_to}" if alert_to else ""
    body = f"올에셋 미니갤 새 글 감지\n\n- 작성자: {post.author}\n- 제목: {post.title}\n- 링크: {post.link}\n\n--- 본문 ---\n{post.body[:4000]}\n\n{mention}"
    payload = {"title": issue_title, "body": body}
    if alert_to: payload["assignees"] = [alert_to]
    requests.post(api_url, headers=headers, json=payload, timeout=30)

def main() -> int:
    list_url = "https://gall.dcinside.com/mini/board/lists/?id=allasset"
    target_nick = "반팔"
    state_file = "state_allasset_banpal.json"
    github_repo = os.getenv("GITHUB_REPOSITORY", "")
    github_token = os.getenv("GITHUB_TOKEN", "")
    alert_to = os.getenv("ALERT_TO", "").strip() or None

    session = requests.Session()
    try:
        html = fetch_html(list_url, session)
        posts = parse_list(html, list_url, target_nick)
    except: return 0

    if not posts: return 0
    state = load_state(state_file)
    last_seen_no = int(state.get("last_seen_no", 0))
    
    if not bool(state.get("initialized", False)):
        save_state(state_file, posts[0].no, initialized=True)
        return 0

    new_posts = sorted([p for p in posts if p.no > last_seen_no], key=lambda p: p.no)
    for post in new_posts:
        try:
            article_html = fetch_html(post.link, session)
            post_full = parse_article(article_html, post)
        except: post_full = post
        
        if github_repo and github_token:
            create_issue(github_repo, github_token, alert_to, post_full)
        
        last_seen_no = post.no
        save_state(state_file, last_seen_no, initialized=True)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
