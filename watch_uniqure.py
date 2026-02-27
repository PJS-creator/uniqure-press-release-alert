import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, List
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

MONTHS = "(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
MONTHS_FULL = "(January|February|March|April|May|June|July|August|September|October|November|December)"
DATE_RE = re.compile(rf"^[\u2022\*\-]?\s*{MONTHS}\s+\d{{1,2}},\s+\d{{4}}\s*$", re.IGNORECASE)
DATE_RE2 = re.compile(rf"^[\u2022\*\-]?\s*{MONTHS_FULL}\s+\d{{1,2}},\s+\d{{4}}\s*$", re.IGNORECASE)

SKIP_TOKENS = {"subscribe", "see all", "learn more", "contact", "menu"}

@dataclass
class PressRelease:
    date: str
    title: str
    url: str
    source_page: str

def fetch_html(url: str, timeout: int = 30) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; uniQure-watcher/1.0; +https://github.com/)"
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

def _clean_lines(text: str) -> List[str]:
    lines = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        ln = ln.replace("\u00a0", " ")
        lines.append(ln)
    return lines

def _is_date_line(line: str) -> bool:
    return bool(DATE_RE.match(line) or DATE_RE2.match(line))

def _looks_like_noise(line: str) -> bool:
    low = line.strip().lower()
    if low in SKIP_TOKENS:
        return True
    if low.startswith("subscribe"):
        return True
    if low.startswith("©") or low.startswith("copyright"):
        return True
    return False

def extract_latest_press_release(html: str, page_url: str) -> Optional[PressRelease]:
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = _clean_lines(text)

    start_candidates = []
    for i, ln in enumerate(lines):
        if "subscribe to receive breaking news alerts" in ln.lower():
            start_candidates.append(i)
    for i, ln in enumerate(lines):
        if ln.strip().lower() == "press releases":
            start_candidates.append(i)

    seen = set()
    start_candidates_unique = []
    for i in start_candidates:
        if i not in seen:
            start_candidates_unique.append(i)
            seen.add(i)

    def parse_from(start_idx: int) -> Optional[PressRelease]:
        chunk = lines[start_idx : start_idx + 200]
        for i, ln in enumerate(chunk):
            if _is_date_line(ln):
                date = ln.strip("•*- ").strip()
                for j in range(i + 1, min(i + 15, len(chunk))):
                    title = chunk[j].strip()
                    if not title or _looks_like_noise(title) or _is_date_line(title):
                        continue

                    href = ""
                    a = soup.find("a", string=lambda s: isinstance(s, str) and title.lower() in s.lower())
                    if a and a.get("href"):
                        href = a["href"]

                    abs_url = urljoin(page_url, href) if href else page_url
                    return PressRelease(date=date, title=title, url=abs_url, source_page=page_url)
        return None

    for idx in start_candidates_unique:
        pr = parse_from(idx)
        if pr:
            return pr

    return parse_from(0)

def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {"last_seen": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"last_seen": ""}

def save_state(path: str, pr: PressRelease) -> None:
    state = {
        "last_seen": f"{pr.date} | {pr.title}",
        "last_seen_date": pr.date,
        "last_seen_title": pr.title,
        "last_seen_url": pr.url,
        "source_page": pr.source_page,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)

def create_issue(repo: str, token: str, pr: PressRelease, alert_to: Optional[str]) -> None:
    api_url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    title = f"[uniQure] New press release: {pr.date} — {pr.title}"
    mention = f"@{alert_to}" if alert_to else ""
    body = "\n".join([
        "새로운 uniQure Press Release가 감지되었습니다.",
        "",
        f"- Date: {pr.date}",
        f"- Title: {pr.title}",
        f"- Link: {pr.url}",
        f"- Source page watched: {pr.source_page}",
        "",
        mention,
    ]).strip() + "\n"

    payload = {"title": title, "body": body}
    if alert_to:
        payload["assignees"] = [alert_to]

    r = requests.post(api_url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"Failed to create issue: {r.status_code} {r.text}")

def main() -> int:
    watch_url = os.getenv("WATCH_URL", "https://www.uniqure.com/investors-media/press-releases")
    fallback_url = os.getenv("FALLBACK_URL", "https://www.uniqure.com/investors-media")
    state_file = os.getenv("STATE_FILE", "state.json")

    github_repo = os.getenv("GITHUB_REPOSITORY", "")
    github_token = os.getenv("GITHUB_TOKEN", "")
    alert_to = os.getenv("ALERT_TO", "").strip() or None

    pr = None
    for url in [watch_url, fallback_url]:
        try:
            html = fetch_html(url)
        except Exception:
            continue
        pr = extract_latest_press_release(html, url)
        if pr:
            break

    if not pr:
        return 0

    state = load_state(state_file)
    last_seen = state.get("last_seen", "")
    current_key = f"{pr.date} | {pr.title}"

    if not last_seen:
        save_state(state_file, pr)
        return 0

    if current_key == last_seen:
        return 0

    if github_repo and github_token:
        create_issue(github_repo, github_token, pr, alert_to)

    save_state(state_file, pr)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
