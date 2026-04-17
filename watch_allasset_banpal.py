import json
import os
import sys
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
    dc_cookie = os.getenv("DC_COOKIE", "")
    if not dc_cookie:
        print("[DEBUG] 🚨 DC_COOKIE 환경변수가 비어있습니다! 깃허브 Secret 설정을 다시 확인해주세요.")
    else:
        print(f"[DEBUG] ✅ 쿠키 로드 성공 (길이: {len(dc_cookie)}자)")
        
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Cookie": dc_cookie
    }
    resp = session.get(url, headers=headers, timeout=timeout)
    print(f"[DEBUG] 🌐 응답 상태 코드: {resp.status_code}")
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
        
        posts.append(DCPost(no=no, title=title, author=nick, link=link, created=""))
    
    posts.sort(key=lambda p: p.no, reverse=True)
    return posts

def main() -> int:
    list_url = "https://gall.dcinside.com/mini/board/lists/?id=allasset"
    target_nick = "반팔"
    state_file = "state_allasset_banpal.json"

    print("=== 디버그 모드 크롤링 시작 ===")
    session = requests.Session()
    
    try:
        html = fetch_html(list_url, session)
        print(f"[DEBUG] 📄 HTML 가져오기 완료 (텍스트 길이: {len(html)})")
        
        # 권한 체크
        if "접근 제한" in html or "접근 권한" in html or "로그인" in html:
            print("[ERROR] 🚫 갤러리 접근 권한이 없습니다! (쿠키 만료, 권한 부족, 또는 봇 차단)")
            return 1
            
        posts = parse_list(html, list_url, target_nick)
        print(f"[DEBUG] 🎯 파싱 완료. 현재 1페이지에서 '{target_nick}' 님의 글을 {len(posts)}개 찾았습니다.")
        
        if not posts:
            print("[DEBUG] 텅 비어있습니다. 해당 닉네임의 글이 현재 1페이지에 없거나, HTML 구조가 다릅니다.")
            return 0
            
    except Exception as e:
        print(f"[CRITICAL ERROR] 💥 파이썬 실행 중 치명적 에러 발생: {e}")
        return 1

    # 이후 이슈 생성 로직은 임시로 주석 처리 (원인 파악이 먼저입니다)
    print("=== 디버그 테스트 완료 ===")
    return 0

if __name__ == "__main__":
    sys.exit(main())
