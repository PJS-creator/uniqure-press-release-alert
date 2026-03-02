import json
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import List, Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


@dataclass
class PressItem:
    title: str
    url: str
    date: str = ""


DATE_RE_EN = re.compile(r"\b([A-Z][a-z]+ \d{1,2}, \d{4})\b")  # e.g., March 02, 2026


def log(msg: str) -> None:
    print(msg, flush=True)


def fetch_html(url: str, timeout: int = 30) -> str:
    # Cache-bust to reduce stale HTML
    sep = "&" if "?" in url else "?"
    bust = f"{sep}_={int(time.time())}"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/123.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    r = requests.get(url + bust, headers=headers, timeout=timeout, allow_redirects=True)
    log(f"[fetch] {url} -> HTTP {r.status_code}, {len(r.text)} bytes")
    r.raise_for_status()
    return r.text


def parse_globenewswire_list(html: str, page_url: str, max_items: int = 20) -> List[PressItem]:
    """
    Parse GlobeNewswire organization search results page:
    https://www.globenewswire.com/search/organization/<...>
    """
    soup = BeautifulSoup(html, "html.parser")

    items: List[PressItem] = []
    seen_urls = set()

    # Find anchors that look like press release links
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        title = a.get_text(" ", strip=True)

        if not href or not title:
            continue

        t = title.lower()
        if t in {"read more", "next page", "page suivante"}:
            continue
        if t.startswith("image:"):
            continue

        if "/news-release/" not in href:
            continue

        full_url = urljoin(page_url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        # Find a date near this link (look up the DOM a bit)
        date = ""
        node = a
        for _ in range(5):
            if node is None or node.parent is None:
                break
            node = node.parent
            blob = node.get_text(" ", strip=True)
            m = DATE_RE_EN.search(blob)
            if m:
                date = m.group(1)
                break

        items.append(PressItem(title=title, url=full_url, date=date))

        if len(items) >= max_items:
            break

    return items


def parse_uniqure_fallback(html: str, page_url: str) -> List[PressItem]:
    """
    Very simple fallback parser for uniqure.com pages that contain a short press release list.
    If the page is JS-rendered and empty, this will return [].
    """
    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]

    # Look for patterns like:
    # Feb 23, 2026
    # uniQure to Announce 2025 Financial Results
    items: List[PressItem] = []
    i = 0
    while i < len(lines):
        m = re.match(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}$", lines[i])
        if m and i + 1 < len(lines):
            date = lines[i]
            title = lines[i + 1].lstrip("# ").strip()
            if title and len(title) > 6:
                items.append(PressItem(title=title, url=page_url, date=date))
                if len(items) >= 5:
                    break
            i += 2
        else:
            i += 1
    return items


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log(f"[state] Failed to read {path}: {e}")
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def gh_create_issue(repo: str, token: str, title: str, body: str, assignee: Optional[str]) -> str:
    url = f"https://api.github.com/repos/{repo}/issues"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "uniqure-watcher",
    }
    payload = {"title": title, "body": body}
    if assignee:
        payload["assignees"] = [assignee]

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub issue create failed: {r.status_code} {r.text}")
    return r.json().get("html_url", "")


def main() -> int:
    watch_url = os.environ.get("WATCH_URL", "").strip()
    fallback_url = os.environ.get("FALLBACK_URL", "").strip()
    state_file = os.environ.get("STATE_FILE", "state.json").strip()
    alert_to = os.environ.get("ALERT_TO", "").strip().lstrip("@")

    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()

    if not watch_url:
        log("[error] WATCH_URL is empty.")
        return 1
    if not repo or not token:
        log("[error] Missing GITHUB_REPOSITORY or GITHUB_TOKEN.")
        return 1

    state = load_state(state_file)
    seen_urls: List[str] = state.get("seen_urls", [])
    seen_set = set(seen_urls)

    # Legacy compatibility: if old state stored a "last_seen_key" like "Feb 23, 2026|Title"
    legacy_key = state.get("last_seen_key") or state.get("last_seen") or ""
    legacy_title = ""
    if isinstance(legacy_key, str) and "|" in legacy_key:
        try:
            _d, legacy_title = legacy_key.split("|", 1)
            legacy_title = legacy_title.strip().lower()
        except Exception:
            legacy_title = ""

    items: List[PressItem] = []

    # Try WATCH_URL first
    try:
        html = fetch_html(watch_url)
        if "globenewswire.com" in watch_url:
            items = parse_globenewswire_list(html, watch_url)
        else:
            items = parse_uniqure_fallback(html, watch_url)
    except Exception as e:
        log(f"[warn] WATCH_URL failed: {e}")

    # If empty, try FALLBACK_URL
    if not items and fallback_url:
        try:
            html = fetch_html(fallback_url)
            if "globenewswire.com" in fallback_url:
                items = parse_globenewswire_list(html, fallback_url)
            else:
                items = parse_uniqure_fallback(html, fallback_url)
        except Exception as e:
            log(f"[warn] FALLBACK_URL failed: {e}")

    if not items:
        log("[result] No press release items found (page may be JS-rendered or blocked).")
        return 0

    log(f"[parse] Found {len(items)} item(s). Latest = {items[0].date} | {items[0].title} | {items[0].url}")

    # Determine new items (top-down until we hit something seen or legacy marker)
    new_items: List[PressItem] = []
    for it in items:
        if it.url in seen_set:
            break
        if legacy_title and it.title.strip().lower() == legacy_title:
            break
        new_items.append(it)

    # First run protection: if we have no seen history and no legacy marker, just initialize.
    if not seen_urls and not legacy_title:
        state["seen_urls"] = [items[0].url]
        state["last_seen_url"] = items[0].url
        save_state(state_file, state)
        log("[init] State initialized. (No alert on very first run.)")
        return 0

    if not new_items:
        log("[result] No new press releases.")
        return 0

    # Create issues oldest -> newest
    created = 0
    for it in reversed(new_items):
        issue_title = f"[uniQure] {it.date} {it.title}".strip()
        body_lines = [
            "New press release detected.",
            "",
            f"- Date: {it.date}" if it.date else "- Date: (unknown)",
            f"- Title: {it.title}",
            f"- Link: {it.url}",
        ]
        if alert_to:
            body_lines.append("")
            body_lines.append(f"cc @{alert_to}")

        issue_url = gh_create_issue(repo, token, issue_title[:240], "\n".join(body_lines), alert_to or None)
        log(f"[issue] Created: {issue_url}")

        # Update seen
        seen_set.add(it.url)
        seen_urls.insert(0, it.url)
        seen_urls = seen_urls[:50]
        created += 1

    state["seen_urls"] = seen_urls
    state["last_seen_url"] = items[0].url
    # Keep legacy keys but update so you can still inspect
    state["last_seen_key"] = f"{items[0].date}|{items[0].title}"
    save_state(state_file, state)

    log(f"[done] Created {created} issue(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
