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
SAVE_CODE_SNIPPETS_AS_SEPARATE_FILES = False  # (미사용)

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

def _tab_label(el, default_name):
    txt = (el.text or "").strip()
    if txt:
        return txt
    for k in ("aria-label", "data-lang", "data-code-lang", "data-language", "title"):
        v = el.get_attribute(k)
        if v:
            return v.strip()
    return default_name

def _nearest_section_container(node):
    """
    탭 컨트롤과 같은 섹션으로 보이는 부모/조상(최대 2단계) 반환,
    실패 시 article 전체 반환.
    """
    try:
        return node.find_element(By.XPATH, "./..")
    except Exception:
        pass
    try:
        return node.find_element(By.XPATH, "./../..")
    except Exception:
        pass
    return driver.find_element(By.TAG_NAME, "article")

def _associated_code_region(tablist):
    """
    탭리스트와 직결된 코드 영역을 최대한 좁게 특정:
    1) 형제 중 다음 코드 후보
    2) 부모 내에서 탭리스트 이후 첫 코드 후보
    3) 폴백: 섹션 컨테이너
    """
    # 1) 탭리스트 바로 뒤쪽 형제에서 코드 후보
    try:
        sibs = tablist.find_elements(
            By.XPATH,
            "following-sibling::*[self::pre or self::code or contains(@class,'devsite-code') or contains(@class,'highlight')][position()<=5]"
        )
        if sibs:
            return sibs[0]
    except Exception:
        pass

    # 2) 부모 컨테이너 안에서 탭리스트 다음 코드 후보
    try:
        parent = tablist.find_element(By.XPATH, "./..")
        candidates = parent.find_elements(
            By.XPATH,
            ".//following::*[self::pre or self::code or contains(@class,'devsite-code') or contains(@class,'highlight')][position()<=8]"
        )
        if candidates:
            return candidates[0]
    except Exception:
        pass

    # 3) 폴백
    return _nearest_section_container(tablist)

def _visible_code_in(container):
    """컨테이너(또는 코드 노드) 안에서 '보이는' 코드 텍스트."""
    try:
        if container.tag_name.lower() in ("pre", "code"):
            candidates = [container]
        else:
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
    속성이 lang_name과 매칭되는 보이는 코드 텍스트.
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

def _visible_code_after(tab_el, section_scope, before_text, wait, lang_name):
    """
    탭 클릭 후 코드 텍스트를 반환.
    1) aria-controls 패널 > 코드
    2) section_scope에서 '변화' 감지
    3) 언어 속성 기반 전역 탐색
    4) 변화가 없어도 현재 보이는 코드 반환
    5) 마지막 폴백: before_text
    """
    # 1) aria-controls 패널 우선
    panel = None
    panel_id = tab_el.get_attribute("aria-controls")
    if panel_id:
        try:
            panel = _retry_stale(lambda: driver.find_element(By.CSS_SELECTOR, f'#{panel_id}'))
            code_blocks = _retry_stale(lambda: panel.find_elements(By.CSS_SELECTOR, "pre, code"))
            texts = [cb.text for cb in code_blocks if cb.is_displayed() and cb.text.strip()]
            if texts:
                return "\n\n".join(texts).strip()
            if panel.text.strip():
                return panel.text.strip()
        except Exception:
            panel = None

    # 2) 변화 대기 (섹션 스코프 기준)
    def changed_code(_):
        txt = _visible_code_in(section_scope)
        return txt if (txt and txt != before_text) else False
    try:
        changed = wait.until(changed_code)
        if changed:
            return changed
    except TimeoutException:
        pass

    # 3) 언어 속성 기반 전역 탐색
    lang_code = _visible_code_by_language(lang_name)
    if lang_code:
        return lang_code

    # 4) 변화가 없어도 현재 보이는 코드 반환
    current = _visible_code_in(section_scope)
    if current:
        return current

    # 5) 마지막 폴백
    return before_text or ""

# ========= 본문/탭 수집 =========
def collect_page_text_with_tabs_and_code_tabs(article_element, wait) -> str:
    """
    - devsite-selector(문서 탭)
    - 언어/코드 탭(라벨만 있고 아래 공용 코드 영역 토글 포함)
    결과 txt에는 탭 순서대로 '언어 → 코드'가 1:1로 들어간다.
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

    # 2) 언어/코드 탭 컨테이너 찾기 (ARIA + 클래스 기반)
    code_pairs_all = []

    # A) 표준 ARIA 탭
    tab_containers = _retry_stale(lambda: article_element.find_elements(By.CSS_SELECTOR, '[role="tablist"]'))

    # B) 클래스 기반 탭 묶음 추가
    class_based = _retry_stale(lambda: article_element.find_elements(
        By.CSS_SELECTOR,
        ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"
    ))
    tab_containers.extend([c for c in class_based if c not in tab_containers])

    for idx, tablist in enumerate(tab_containers, start=1):
        try:
            # 후보 탭 버튼들 수집
            tabs = []
            tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
            tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li > button, li > a"))

            # 보이는 것만, 중복 제거
            seen = set()
            uniq_tabs = []
            for t in tabs:
                if not t.is_displayed():
                    continue
                k = getattr(t, "_id", id(t))
                if k in seen:
                    continue
                seen.add(k)
                uniq_tabs.append(t)

            if not uniq_tabs:
                continue

            # 탭리스트에 결합된 '가장 가까운 코드 영역/스코프'
            section_scope = _associated_code_region(tablist)

            for t_i in range(len(uniq_tabs)):
                # 클릭 직전 재조회(새 DOM 반영)
                try:
                    tabs_now = []
                    tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li > button, li > a"))
                    tabs_now = [t for t in tabs_now if t.is_displayed()]
                    tab = tabs_now[t_i]
                except Exception:
                    continue

                lang_name = _tab_label(tab, f"Tab {idx}-{t_i+1}")

                try:
                    # 클릭 전 스냅샷
                    before_text = _visible_code_in(section_scope)

                    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                    driver.execute_script("arguments[0].click();", tab)

                    # 선택 상태 대기(있는 경우)
                    try:
                        def selected(_):
                            val = tab.get_attribute("aria-selected")
                            return (val == "true") if val is not None else True
                        wait.until(selected)
                    except TimeoutException:
                        pass

                    snippet = _visible_code_after(tab, section_scope, before_text, wait, lang_name)

                    if snippet:
                        code_pairs_all.append((lang_name, snippet))
                    else:
                        print(f"[tabs] '{lang_name}' 코드 스니펫을 찾지 못함")

                except Exception as e:
                    print(f"[tabs] '{lang_name}' 클릭/수집 오류: {e}")
                    continue

        except Exception as e:
            print(f"[tablist-like] 처리 오류: {e}")

    # 언어-코드 쌍을 탭 순서대로 합쳐서 본문에 삽입
    if code_pairs_all:
        formatted = []
        for lang, code in code_pairs_all:
            formatted.append(f"언어: {lang}\n{code}")
        parts.append("=== 코드 탭 (언어 → 코드) ===\n" + "\n\n".join(formatted))

    # 3) 일반 본문(탭/코드 위젯 제외) — 탭 조작 이후 fresh 조회
    try:
        fresh_article = driver.find_element(By.TAG_NAME, "article")
        nodes = fresh_article.find_elements(
            By.CSS_SELECTOR,
            (
                ":scope > :not(devsite-selector)"
                ":not([role='tablist']):not([role='tabpanel'])"
                ":not(.devsite-code):not(.highlight)"
            )
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
