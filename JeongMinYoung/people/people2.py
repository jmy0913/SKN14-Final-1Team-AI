import os
import re
import time
from collections import deque
from urllib.parse import (
    urljoin, urlparse, urldefrag, urlunparse, urlencode, parse_qs
)

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
OUTPUT_DIR = "people_docs_crawled"  # 저장 폴더명
MAX_PAGES  = 500
CRAWL_DELAY_SEC = 1

# 페이지당 txt 하나만 생성 (언어→코드 쌍은 txt 안에 포함)
SAVE_CODE_SNIPPETS_AS_SEPARATE_FILES = False  # 안전용(현재 미사용)

# ========= 크롤 대상 제한 =========
ALLOW_DOMAINS = {"developers.google.com"}
ALLOW_PATH_PREFIXES = (
    "/people/api/rest",  # REST 참조
    "/people/v1/",       # v1
    "/people/docs/",     # docs
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
    """ALLOW_DOMAINS + 허용 prefix + hl=ko 만 통과"""
    if not url:
        return False
    if url.startswith(("javascript:", "mailto:", "tel:")):
        return False

    parsed = urlparse(url)
    if parsed.netloc and parsed.netloc not in ALLOW_DOMAINS:
        return False

    path = parsed.path or ""
    if not any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
        return False

    qs = parse_qs(parsed.query)
    lang = qs.get("hl", [None])[0]
    return lang == "ko"

def force_hl_ko(url: str) -> str:
    """허용 경로면 쿼리에 hl=ko 강제 삽입/교체."""
    parsed = urlparse(url)
    path = parsed.path or ""
    if any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
        qs = parse_qs(parsed.query)
        qs["hl"] = ["ko"]
        new_query = urlencode(qs, doseq=True)
        return urlunparse(parsed._replace(query=new_query))
    return url

def normalize_url(url: str) -> str:
    """해시 제거 + hl=ko 강제(허용 경로만)."""
    url, _ = urldefrag(url)
    return force_hl_ko(url)

def url_to_safe_filename(url: str) -> str:
    """URL -> 안전한 파일명 (쿼리도 반영; hl=ko 포함 가능)"""
    path = url.split("?")[0].replace(BASE_URL, "")
    fname = re.sub(r'[/\\?%*:|"<>]', "_", path).strip("_") or "index"
    parsed = urlparse(url)
    if parsed.query:
        q = re.sub(r'[^A-Za-z0-9=&._-]', "_", parsed.query)
        if q:
            fname += f"__{q}"
    return fname + ".txt"

def extract_all_page_links() -> list:
    """현재 DOM의 모든 <a href> 절대 URL 반환"""
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    out = []
    for a in anchors:
        href = a.get_attribute("href")
        if not href:
            continue
        out.append(urljoin(driver.current_url, href))
    return out

# ========= stale 방지/탭 헬퍼 =========
def _retry_stale(fn, tries=3, sleep=0.2):
    """StaleElementReferenceException 대비 재시도 래퍼"""
    for i in range(tries):
        try:
            return fn()
        except StaleElementReferenceException:
            if i == tries - 1:
                raise
            time.sleep(sleep)

def _nearest_section_container(tablist):
    """
    언어 탭(라벨 목록)과 같은 섹션에 있는 코드 영역을 추정.
    일반적으로 tablist의 부모/조상 컨테이너가 코드 블록을 포함.
    """
    try:
        return tablist.find_element(By.XPATH, "./..")
    except Exception:
        pass
    try:
        return tablist.find_element(By.XPATH, "./../..")
    except Exception:
        pass
    # 폴백: 페이지의 article 전체
    return driver.find_element(By.TAG_NAME, "article")

def _visible_code_in(container):
    """컨테이너 안에서 현재 보이는 코드 블록 텍스트를 반환(없으면 빈 문자열)."""
    try:
        candidates = container.find_elements(
            By.CSS_SELECTOR,
            "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code"
        )
        texts = [c.text for c in candidates if c.is_displayed() and c.text.strip()]
        if texts:
            return "\n\n".join(texts).strip()
    except Exception:
        pass
    return ""

def _visible_code_by_language(lang_name):
    """
    페이지 전역에서 data-language / data-code-lang / data-lang
    속성이 lang_name과 매칭되는 보이는 코드 텍스트를 찾아 반환.
    """
    lang_like = (lang_name or "").lower().strip()
    selectors = [
        f'[data-language="{lang_like}"] pre',
        f'[data-language="{lang_like}"] code',
        f'[data-code-lang="{lang_like}"] pre',
        f'[data-code-lang="{lang_like}"] code',
        f'[data-lang="{lang_like}"] pre',
        f'[data-lang="{lang_like}"] code',
    ]
    for sel in selectors:
        try:
            nodes = driver.find_elements(By.CSS_SELECTOR, sel)
            texts = [n.text for n in nodes if n.is_displayed() and n.text.strip()]
            if texts:
                return "\n\n".join(texts).strip()
        except Exception:
            continue
    return ""

# ========= 본문/탭 수집 =========
def collect_page_text_with_tabs_and_code_tabs(article_element, wait) -> str:
    """
    - devsite-selector(문서 탭)
    - role='tablist'(언어/코드 탭; 라벨만 있고 아래 공용 코드 영역이 토글되는 구조 포함)
    를 모두 처리.
    ✅ 결과 txt에는 탭 순서대로 '언어 → 코드'가 1:1로 들어간다.
    """
    parts = []

    # 1) devsite-selector (문서 섹션 탭)
    selectors = _retry_stale(lambda: article_element.find_elements(By.CSS_SELECTOR, "devsite-selector"))
    for selector in selectors:
        tab_texts = []
        try:
            tabs = _retry_stale(lambda: selector.find_elements(By.CSS_SELECTOR, "tab > a"))
            tab_names = [t.text.strip() or f"Tab {i+1}" for i, t in enumerate(tabs)]

            for i in range(len(tab_names)):
                try:
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

    # 2) role="tablist" (언어/코드 탭) — 라벨 → 공용 코드 영역 토글 대응
    code_pairs_all = []  # [(lang, code)] (페이지 내 순서대로)

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

            # 탭리스트와 같은 섹션 컨테이너 추정
            section_container = _nearest_section_container(tablist)

            for t_i in range(len(tabs)):
                tabs = _retry_stale(lambda: tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                tab = tabs[t_i]
                lang_name = tab_label(tab)

                try:
                    # 클릭 전 현재 보이는 코드 스냅샷
                    before_text = _visible_code_in(section_container)

                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                    driver.execute_script("arguments[0].click();", tab)

                    # 우선 aria-controls 패널 시도
                    snippet = ""
                    panel_id = tab.get_attribute("aria-controls")
                    if panel_id:
                        try:
                            panel = _retry_stale(lambda: article_element.find_element(By.CSS_SELECTOR, f'#{panel_id}'))
                            code_blocks = _retry_stale(lambda: panel.find_elements(By.CSS_SELECTOR, "pre, code"))
                            texts = [cb.text for cb in code_blocks if cb.is_displayed() and cb.text.strip()]
                            snippet = "\n\n".join(texts).strip() if texts else (panel.text.strip() or "")
                        except Exception:
                            snippet = ""

                    # 패널이 없거나 비어있으면, 같은 섹션에서 '보이는 코드'가 바뀔 때까지 대기
                    if not snippet:
                        def changed_code(_):
                            txt = _visible_code_in(section_container)
                            return txt if (txt and txt != before_text) else False
                        try:
                            snippet = wait.until(changed_code)
                        except TimeoutException:
                            # 그래도 없으면, 언어 속성 기반 전역 탐색(폴백)
                            snippet = _visible_code_by_language(lang_name)

                    if snippet:
                        code_pairs_all.append((lang_name, snippet))
                    else:
                        print(f"[tabs] '{lang_name}' 코드 스니펫을 찾지 못함")

                except Exception as e:
                    print(f"[tabs] '{lang_name}' 클릭/수집 오류: {e}")
                    continue

        except Exception as e:
            print(f"[tablist] 처리 오류: {e}")

    # 언어-코드 쌍을 탭 순서대로 합쳐서 본문에 삽입
    if code_pairs_all:
        formatted = []
        for lang, code in code_pairs_all:
            formatted.append(f"언어: {lang}\n{code}")
        parts.append("=== 코드 탭 (언어 → 코드) ===\n" + "\n\n".join(formatted))

    # 3) 일반 본문(탭 위젯 제외) — 탭 조작 이후 fresh 조회
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
    # 시작 URL도 정규화(hl=ko 보정)
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

            page_filename = url_to_safe_filename(url)
            page_text = collect_page_text_with_tabs_and_code_tabs(article, wait)

            # 페이지당 txt 하나 저장
            filepath = os.path.join(OUTPUT_DIR, page_filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {driver.current_url}\n\n{page_text}")
            print(f"저장 완료: {filepath}")

            visited.add(url)
            pages_crawled += 1

            # 링크 추출(페이지 전체) → hl=ko 보정 → 필터
            raw_links = extract_all_page_links()
            next_links = []
            for raw in raw_links:
                abs_url = urljoin(driver.current_url, raw)
                norm = normalize_url(abs_url)  # 여기서 hl=ko 강제
                if not is_allowed_link(norm):
                    continue
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
