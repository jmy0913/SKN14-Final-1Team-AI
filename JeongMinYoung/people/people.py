
import os
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    StaleElementReferenceException,
    TimeoutException,
)

# ========= 기본 설정 =========
BASE_URL   = "https://developers.google.com"
OUTPUT_DIR = "people_docs_crawled"
MAX_PAGES  = 500
CRAWL_DELAY_SEC = 1

# 코드 스니펫을 별도 파일로 저장하지 않음(페이지별 txt 하나만 생성)
SAVE_CODE_SNIPPETS_AS_SEPARATE_FILES = False

# ========= 크롤 대상 제한(요청하신 설정) =========
ALLOW_DOMAINS = {"developers.google.com"}
ALLOW_PATH_PREFIXES = (
    "/people/api/rest",  # 기존 API 경로
    "/people/v1/",       # v1 API
    "/people/docs/",     # docs 경로
    "/people/"
)
START_URLS = [
    "https://developers.google.com/people?hl=ko"
]

# ========= 준비 =========
os.makedirs(OUTPUT_DIR, exist_ok=True)

chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--log-level=3")
service = ChromeService()
driver = webdriver.Chrome(service=service, options=chrome_options)
wait = WebDriverWait(driver, 15)

# ========= 유틸 =========
def is_allowed_link(url: str) -> bool:
    """ALLOW_DOMAINS 안 + 경로가 ALLOW_PATH_PREFIXES 중 하나로 시작"""
    if not url:
        return False
    if url.startswith(("javascript:", "mailto:", "tel:")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc not in ALLOW_DOMAINS:
        return False

    path = parsed.path or ""
    return any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES)

def normalize_url(url: str) -> str:
    """해시 제거 (쿼리는 유지)"""
    url, _frag = urldefrag(url)
    return url

def url_to_safe_filename(url: str) -> str:
    """URL -> 안전한 파일명"""
    path = url.split("?")[0].replace(BASE_URL, "")
    fname = re.sub(r'[/\\?%*:|"<>]', "_", path).strip("_") or "index"
    parsed = urlparse(url)
    if parsed.query:
        q = re.sub(r'[^A-Za-z0-9=&._-]', "_", parsed.query)
        if q:
            fname += f"__{q}"
    return fname + ".txt"

def extract_all_page_links() -> list:
    """현재 DOM의 모든 <a href> 절대 URL로 반환"""
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    out = []
    for a in anchors:
        href = a.get_attribute("href")
        if not href:
            continue
        out.append(urljoin(driver.current_url, href))
    return out

# ========= 본문/탭 수집 =========
def _retry_stale(fn, tries=3, sleep=0.2):
    """StaleElementReferenceException 대비 재시도 래퍼"""
    for i in range(tries):
        try:
            return fn()
        except StaleElementReferenceException:
            if i == tries - 1:
                raise
            time.sleep(sleep)

def collect_page_text_with_tabs_and_code_tabs(article_element, wait) -> str:
    """
    - devsite-selector(문서 섹션 탭)
    - role='tablist'(언어/코드 탭)
    를 클릭해가며 수집.
    ✅ 결과 txt에는 탭 순서대로: "언어 → 코드" 1:1 연결 형태로 들어감.
    """
    parts = []

    # -----------------------------
    # 1) devsite-selector (문서 섹션 탭)
    # -----------------------------
    selectors = _retry_stale(lambda: article_element.find_elements(By.CSS_SELECTOR, "devsite-selector"))
    for selector in selectors:
        tab_texts = []
        try:
            tabs = _retry_stale(lambda: selector.find_elements(By.CSS_SELECTOR, "tab > a"))
            tab_names = [t.text.strip() or f"Tab {i+1}" for i, t in enumerate(tabs)]

            for i in range(len(tab_names)):
                try:
                    # 클릭 직전에 다시 찾아서 클릭 (stale 방지)
                    btns = _retry_stale(lambda: selector.find_elements(By.CSS_SELECTOR, "tab > a"))
                    btn = btns[i]
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", btn)
                    driver.execute_script("arguments[0].click();", btn)
                    time.sleep(0.3)

                    active_panel = _retry_stale(
                        lambda: selector.find_element(By.CSS_SELECTOR, "section[role='tabpanel'].devsite-active")
                    )
                    tab_texts.append(f"--- 탭: {tab_names[i]} ---\n{active_panel.text}")
                except Exception as e:
                    print(f"[devsite-selector] 탭 처리 오류: {e}")
                    continue
        except Exception as e:
            print(f"[devsite-selector] 탐색 오류: {e}")

        if tab_texts:
            parts.append("\n\n".join(tab_texts))

    # -----------------------------
    # 2) role="tablist" (언어/코드 탭)
    #    👉 여기서 언어-코드 쌍을 '탭 순서대로' 모아서 나중에 묶어서 출력
    # -----------------------------
    code_pairs_all = []  # [(lang, code)] 를 페이지 내 순서대로 누적

    tablists = _retry_stale(lambda: article_element.find_elements(By.CSS_SELECTOR, '[role="tablist"]'))
    for idx, tablist in enumerate(tablists, start=1):
        try:
            tabs = _retry_stale(lambda: tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))

            def tab_label(el):
                txt = el.text.strip()
                if txt: return txt
                for k in ("aria-label", "data-lang", "data-code-lang", "data-language", "title"):
                    v = el.get_attribute(k)
                    if v: return v
                return f"Tab {idx}"

            for t_i in range(len(tabs)):
                # 클릭 직전에 다시 가져오기 (stale 방지)
                tabs = _retry_stale(lambda: tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                tab = tabs[t_i]
                lang_name = tab_label(tab)
                try:
                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                    driver.execute_script("arguments[0].click();", tab)

                    # 패널 찾기 (aria-controls 우선)
                    panel = None
                    panel_id = tab.get_attribute("aria-controls")
                    if panel_id:
                        try:
                            panel = _retry_stale(lambda: article_element.find_element(By.CSS_SELECTOR, f'#{panel_id}'))
                        except Exception:
                            panel = None

                    # fallback: 가까운 컨테이너 내 보이는 role=tabpanel
                    if panel is None:
                        container = _retry_stale(lambda: tablist.find_element(By.XPATH, "./.."))
                        candidates = _retry_stale(lambda: container.find_elements(By.CSS_SELECTOR, '[role="tabpanel"]'))

                        def visible_any(_):
                            vis = [c for c in candidates if c.is_displayed()]
                            return vis[0] if vis else False

                        try:
                            panel = wait.until(visible_any)
                        except TimeoutException:
                            time.sleep(0.5)
                            candidates = _retry_stale(lambda: container.find_elements(By.CSS_SELECTOR, '[role="tabpanel"]'))
                            vis2 = [c for c in candidates if c.is_displayed()]
                            panel = vis2[0] if vis2 else None

                    if panel is None:
                        print(f"[tabs] 패널을 찾지 못함: {lang_name}")
                        continue

                    # 코드 우선 수집, 없으면 텍스트
                    code_blocks = _retry_stale(lambda: panel.find_elements(By.CSS_SELECTOR, "pre, code"))
                    if code_blocks:
                        code_text = "\n\n".join(cb.text for cb in code_blocks if cb.is_displayed()).strip()
                        snippet = code_text or panel.text.strip()
                    else:
                        snippet = panel.text.strip()

                    if snippet:
                        code_pairs_all.append((lang_name, snippet))

                except Exception as e:
                    print(f"[tabs] '{lang_name}' 클릭/수집 오류: {e}")
                    continue
        except Exception as e:
            print(f"[tablist] 처리 오류: {e}")

    # 👉 언어-코드 쌍을 한 번에, 탭 순서대로 묶어서 txt에 넣는다.
    if code_pairs_all:
        formatted_pairs = []
        for lang, code in code_pairs_all:
            formatted_pairs.append(f"언어: {lang}\n{code}")
        parts.append("=== 코드 탭 (언어 → 코드) ===\n" + "\n\n".join(formatted_pairs))

    # -----------------------------
    # 3) 일반 본문 텍스트 (탭 위젯 제외)
    #    탭 클릭 등 DOM 변경 이후 article을 '다시' 찾아서 읽음
    # -----------------------------
    try:
        fresh_article = driver.find_element(By.TAG_NAME, "article")
        nodes = fresh_article.find_elements(
            By.CSS_SELECTOR,
            ":scope > :not(devsite-selector):not([role='tablist']):not([role='tabpanel'])"
        )
        for node in nodes:
            try:
                txt = _retry_stale(lambda: node.text.strip())
                if txt:
                    parts.append(txt)
            except StaleElementReferenceException:
                time.sleep(0.1)
                try:
                    txt = node.text.strip()
                    if txt:
                        parts.append(txt)
                except Exception:
                    print("[본문] 해당 노드 재시도 실패(무시)")
                    continue
    except Exception as e:
        print(f"[본문] 수집 오류(최종): {e}")

    return "\n\n".join(filter(None, parts)).strip()

# ========= 크롤 메인 루프 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited = set()
    discovered = set(q)
    pages_crawled = 0

    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited:
            continue

        print(f"\n({pages_crawled+1}) 크롤링: {url}")
        try:
            driver.get(url)
            article = wait.until(EC.presence_of_element_located((By.TAG_NAME, "article")))

            # 본문 + 탭 + 코드 탭 수집 (언어→코드 순서 보장)
            page_filename = url_to_safe_filename(url)
            page_text = collect_page_text_with_tabs_and_code_tabs(article, wait)

            # 본문 저장 (페이지당 txt 하나)
            filepath = os.path.join(OUTPUT_DIR, page_filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {driver.current_url}\n\n{page_text}")
            print(f"저장 완료: {filepath}")

            visited.add(url)
            pages_crawled += 1

            # 링크 추출(페이지 전체)
            raw_links = extract_all_page_links()
            next_links = []
            for raw in raw_links:
                abs_url = urljoin(driver.current_url, raw)
                if not is_allowed_link(abs_url):
                    continue
                norm = normalize_url(abs_url)
                if norm not in visited and norm not in discovered:
                    next_links.append(norm)

            if next_links:
                q.extend(next_links)
                discovered.update(next_links)
                print(f"  ↳ 새 링크 {len(next_links)}개 추가 (대기열 {len(q)}개)")

        except Exception as e:
            print(f"페이지 처리 중 오류: {url} - {e}")

        time.sleep(CRAWL_DELAY_SEC)

    print(f"\n✅ 완료: 총 {pages_crawled} 페이지 크롤링 (상한 {MAX_PAGES})")

# ========= 실행 =========
try:
    crawl()
finally:
    driver.quit()
    print("브라우저 종료")
