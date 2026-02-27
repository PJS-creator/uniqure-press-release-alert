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


def fetch_html(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; dcinside-watcher/1.0; +https://github.com/)"
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = resp.apparent_encoding or "utf-8"
    return resp.text


def parse_list(html: str, list_url: str, target_nick: str) -> List[DCPost]:
    soup = BeautifulSoup(html, "html.parser")

    # 게시글 리스트 tr 모음
    rows = soup.select("table.gall_list tbody tr")
    if not rows:
        rows = soup.select("tbody tr")

    posts: List[DCPost] = []

    for tr in rows:
        # 글 번호 (data-no 속성)
        no_attr = tr.get("data-no")
        if not no_attr:
            continue
        try:
            no = int(str(no_attr).strip())
        except ValueError:
            continue

        # 글쓴이 셀
        writer_td = tr.find("td", class_="gall_writer")
        if not writer_td:
            continue

        # 닉네임 추출
        nick = writer_td.get("data-nick", "").strip()
        if not nick:
            nick_el = writer_td.find(class_="nickname")
            if nick_el:
                nick = nick_el.get_text(strip=True)
            else:
                nick = writer_td.get_text(strip=True)

        nick = nick.strip()
        if nick != target_nick:
            continue

        # 제목 + 링크
        title_a = tr.select_one("td.gall_tit a")
        if not title_a:
            continue

        title = title_a.get_text(strip=True)
        href = title_a.get("href", "")
        link = urljoin(list_url, href) if href else list_url

        # 작성일
        date_td = tr.find("td", class_="gall_date")
        created = ""
        if date_td:
            created = (date_td.get("title") or date_td.get_text(strip=True)).strip()

        posts.append(DCPost(no=no, title=title, author=nick, link=link, created=created))

    # 글 번호 큰 것(최신)부터 정렬
    posts.sort(key=lambda p: p.no, reverse=True)
    return posts


def parse_article(html: str, post: DCPost) -> DCPost:
    soup = BeautifulSoup(html, "html.parser")

    # 제목 (본문 페이지 실제 제목으로 한번 더 보정)
    title_el = soup.select_one(".title_subject")
    if title_el:
        post.title = title_el.get_text(strip=True)

    # 본문
    body_el = soup.select_one(".write_div")
    if body_el:
        body_text = body_el.get_text("\n", strip=True)
    else:
        body_text = ""
    post.body = body_text
    return post


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_seen_no": 0, "initialized": False}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {"last_seen_no": 0, "initialized": False}
    if "initialized" not in data:
        data["initialized"] = True
    return data


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
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    issue_title = f"[DCInside] {post.author} 새 글: {post.title}"

    # 너무 긴 글은 잘라서 올리기 (기본 4000자)
    max_body_chars = int(os.getenv("MAX_BODY_CHARS", "4000"))
    body_text = post.body or ""
    if len(body_text) > max_body_chars:
        body_text_snippet = (
            body_text[:max_body_chars].rstrip()
            + "\n\n...(글이 너무 길어서 여기까지 잘렸습니다. 나머지는 링크에서 확인하세요)..."
        )
    else:
        body_text_snippet = body_text

    mention = f"@{alert_to}" if alert_to else ""

    body_lines = [
        "디시인사이드 유니큐어 미니갤에서 새 글이 감지되었습니다.",
        "",
        f"- 글 번호(no): {post.no}",
        f"- 글쓴이: {post.author}",
        f"- 제목: {post.title}",
        f"- 작성일: {post.created}",
        f"- 링크: {post.link}",
        "",
        "--- 본문 ---",
        body_text_snippet,
        "",
        mention,
    ]
    body = "\n".join(body_lines).strip() + "\n"

    payload = {"title": issue_title, "body": body}
    if alert_to:
        payload["assignees"] = [alert_to]

    r = requests.post(api_url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Failed to create issue: {r.status_code} {r.text}")


def main() -> int:
    # 유니큐어 미니갤 리스트 URL
    list_url = os.getenv(
        "DC_LIST_URL",
        "https://gall.dcinside.com/mini/board/lists/?id=uniqure",
    )
    # 감시할 닉네임
    target_nick = os.getenv("TARGET_NICK", "ㅎㅇ").strip()
    state_file = os.getenv("STATE_FILE", "state_dcinside_ha.json")

    github_repo = os.getenv("GITHUB_REPOSITORY", "")
    github_token = os.getenv("GITHUB_TOKEN", "")
    alert_to = os.getenv("ALERT_TO", "").strip() or None

    if not target_nick:
        return 0

    try:
        html = fetch_html(list_url)
    except Exception:
        return 0

    posts = parse_list(html, list_url, target_nick)
    if not posts:
        return 0

    state = load_state(state_file)
    last_seen_no = int(state.get("last_seen_no", 0))
    initialized = bool(state.get("initialized", False))

    # 첫 실행: 기준값만 저장하고 알림은 안 보냄 (스팸 방지)
    if not initialized:
        latest_no = posts[0].no
        save_state(state_file, latest_no, initialized=True)
        return 0

    # 새 글(번호가 더 큰 것들)만 추림
    new_posts = [p for p in posts if p.no > last_seen_no]
    if not new_posts:
        return 0

    # 오래된 것부터 이슈 생성
    new_posts_sorted = sorted(new_posts, key=lambda p: p.no)

    for post in new_posts_sorted:
        try:
            article_html = fetch_html(post.link)
            post_full = parse_article(article_html, post)
        except Exception:
            post_full = post

        if github_repo and github_token:
            create_issue(github_repo, github_token, alert_to, post_full)

        if post.no > last_seen_no:
            last_seen_no = post.no
            save_state(state_file, last_seen_no, initialized=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
