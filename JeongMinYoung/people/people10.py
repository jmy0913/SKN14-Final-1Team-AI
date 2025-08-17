import os
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse, urlencode, parse_qs

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException

# ========= 기본 설정 =========
BASE_URL = "https://developers.google.com"
OUTPUT_DIR = "people_docs_fast_percell"
MAX_PAGES = 500
CRAWL_DELAY_SEC = 2  # 빠른 모드

# ========= 크롤 제한 =========
ALLOW_DOMAINS = {"developers.google.com"}
ALLOW_PATH_PREFIXES = (
    "/people/api/rest",
    "/people/v1/",
    "/people/docs/",
    "/people/",
)
START_URLS = ["https://developers.google.com/people?hl=ko"]

# ========= 언어 라벨 매핑 =========
LANGUAGE_ALIASES = {
    "자바": ["java"], "파이썬": ["python", "py"], "프로토콜": ["http", "rest", "protocol"],
    "자바스크립트": ["javascript", "js", "node", "nodejs", "node.js"],
    "java": ["java"], "python": ["python", "py"], "php": ["php"], "ruby": ["ruby"],
    "node.js": ["node", "nodejs", "node.js", "javascript", "js"],
    "nodejs": ["node", "nodejs", "node.js", "javascript", "js"],
    ".net": ["csharp", "dotnet", "cs", "c#"], "net": ["csharp", "dotnet", "cs", "c#"],
    "c#": ["csharp", "dotnet", "cs", "c#"], "dotnet": ["csharp", "dotnet", "cs", "c#"],
    "objc": ["objective-c", "objc"], "obj-c": ["objective-c", "objc"], "objective-c": ["objective-c", "objc"],
    "swift": ["swift"], "kotlin": ["kotlin"], "go": ["go", "golang"],
}
def _lang_candidates(label: str):
    key = (label or "").strip().lower()
    key_norm = re.sub(r"[()\[\]\s\.\-–—·:+]+", "", key)
    return LANGUAGE_ALIASES.get(key_norm, LANGUAGE_ALIASES.get(key, [key_norm or key]))

# ========= 준비 =========
os.makedirs(OUTPUT_DIR, exist_ok=True)

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1366,768")
    chrome_options.page_load_strategy = 'none'  # 필요한 것만 기다림

    # 이미지 로딩 차단
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")

    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    service = ChromeService()
    drv = webdriver.Chrome(service=service, options=chrome_options)
    drv.set_page_load_timeout(45)
    drv.implicitly_wait(0)   # 명시적 대기만
    return drv

driver = setup_driver()
wait = WebDriverWait(driver, 12)

# ========= 공통 정규화 =========
def _norm_text(t: str) -> str:
    if not t:
        return ""
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()

# ========= URL/링크 유틸 =========
def is_allowed_link(url: str) -> bool:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    try:
        p = urlparse(url)
        if p.netloc and p.netloc not in ALLOW_DOMAINS:
            return False
        path = p.path or ""
        if not any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
            return False
        qs = parse_qs(p.query)
        return qs.get("hl", [None])[0] == "ko"
    except Exception:
        return False

def force_hl_ko(url: str) -> str:
    try:
        p = urlparse(url)
        if any((p.path or "").startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
            qs = parse_qs(p.query); qs["hl"] = ["ko"]
            return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
        return url
    except Exception:
        return url

def normalize_url(url: str) -> str:
    try:
        url, _ = urldefrag(url)
        return force_hl_ko(url)
    except Exception:
        return url

def url_to_safe_filename(url: str) -> str:
    try:
        path = url.split("?")[0].replace(BASE_URL, "")
        fname = re.sub(r'[/\\?%*:|"<>]', "_", path).strip("_") or "index"
        p = urlparse(url)
        if p.query:
            q = re.sub(r'[^A-Za-z0-9=&._-]', "_", p.query)
            if q: fname += f"__{q}"
        return fname + ".txt"
    except Exception:
        return f"page_{int(time.time())}.txt"

def extract_all_page_links() -> list:
    try:
        hrefs = driver.execute_script("""
const out = [];
document.querySelectorAll('article a[href], main a[href], nav a[href]').forEach(a=>{
  const h = a.getAttribute('href'); if(h) out.push(h);
});
return out;
        """) or []
        abs_links = [urljoin(driver.current_url, h) for h in hrefs]
        return [normalize_url(u) for u in abs_links if is_allowed_link(normalize_url(u))]
    except Exception:
        return []

# ========= 탭/코드 헬퍼 =========
def _tab_label(el, default_name):
    try:
        txt = (el.text or "").strip()
        if txt: return txt
        for k in ("aria-label","data-lang","data-code-lang","data-language","title"):
            v = el.get_attribute(k)
            if v: return v.strip()
        return default_name
    except Exception:
        return default_name

def _closest_selector_host(el):
    try:
        return el.find_element(By.XPATH, "ancestor::devsite-selector[1]")
    except Exception:
        return None

def _find_panel_for_tab(tab, tablist):
    try:
        pid = tab.get_attribute("aria-controls")
        if not pid: return None
        host = _closest_selector_host(tablist)
        if host:
            try:
                return host.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
            except Exception:
                pass
        try:
            return tablist.find_element(By.XPATH, f"following::section[@role='tabpanel' and @id='{pid}'][1]")
        except Exception:
            pass
        try:
            return driver.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
        except Exception:
            return None
    except Exception:
        return None

# === 코드 셀 단위 스냅샷 ===
def _list_code_cells(root):
    """패널(root) 안의 개별 코드 셀 요소 목록을 반환"""
    try:
        cells = driver.execute_script("""
const root = arguments[0];
const seen = new Set(); const arr = [];
function push(el){ if(el && !seen.has(el)){ seen.add(el); arr.push(el); } }
// devsite 컴포넌트(그림자 루트)
root.querySelectorAll('devsite-code,devsite-snippet').forEach(push);
// 일반 pre/code (devsite-code 내부는 제외)
root.querySelectorAll('pre,code,div.highlight pre,div.devsite-code pre,div.devsite-code code').forEach(el=>{
  if(!el.closest('devsite-code,devsite-snippet')) push(el);
});
return arr;
        """, root)
        return cells or []
    except Exception:
        return []

def _grab_cell_text(cell):
    """하나의 코드 셀에서 텍스트 추출"""
    try:
        txt = driver.execute_script("""
const el = arguments[0];
function collectFrom(host){
  const out = [];
  const sr = host.shadowRoot; if(!sr) return "";
  sr.querySelectorAll('pre, code').forEach(n=>{
    const t=(n.innerText||n.textContent||"").trim();
    if(t) out.push(t);
  });
  return out.join("\\n\\n");
}
const tag = (el.tagName||"").toLowerCase();
if(tag === 'devsite-code' || tag === 'devsite-snippet'){
  return collectFrom(el);
} else {
  return (el.innerText||el.textContent||"").trim();
}
        """, cell)
        return _norm_text(txt)
    except Exception:
        return ""

def _snapshot_per_cell(root):
    cells = _list_code_cells(root)
    texts = [ _grab_cell_text(c) for c in cells ]
    return texts  # 인덱스 = 셀 번호

def _panel_text_len(root):
    try:
        return int(driver.execute_script("return (arguments[0].innerText||'').length", root) or 0)
    except Exception:
        return 0

def _collect_snippets_for_tab(tab, tablist, lang_label):
    results = []
    snap_root = _find_panel_for_tab(tab, tablist) or tablist

    # 전/후: 각 '셀' 기준으로 비교
    try:
        before = _snapshot_per_cell(snap_root)
        prev_len = _panel_text_len(snap_root)

        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
        driver.execute_script("arguments[0].click();", tab)

        # 패널 텍스트 길이 변화 또는 aria-selected로 짧게 대기
        try:
            WebDriverWait(driver, 6).until(
                lambda _: (tab.get_attribute("aria-selected") == "true")
                         or (tab.get_attribute("aria-selected") is None)
                         or (_panel_text_len(snap_root) != prev_len)
            )
        except TimeoutException:
            pass

        after = _snapshot_per_cell(snap_root)

        # 셀 개수에 맞춰 비교(초과/부족에 안전)
        m = max(len(before), len(after))
        changed_any = False
        for i in range(m):
            b = before[i] if i < len(before) else ""
            a = after[i]  if i < len(after)  else ""
            if a and a != b:
                changed_any = True
                results.append((f"{lang_label} · 셀#{i+1}", a, True))

        if not changed_any:
            # 변경 감지 실패 시, 패널 전체를 폴백(셀별로 그대로 출력)
            for i, a in enumerate(after, 1):
                if a:
                    results.append((f"{lang_label} · 셀#{i}", a, True))
        return results
    except Exception:
        return results

# ========= 섹션 분리 =========
def _iter_section_scopes(article):
    try:
        body = article.find_element(By.CSS_SELECTOR, ".devsite-article-body")
    except Exception:
        body = article
    try:
        children = body.find_elements(By.XPATH, "./*")
    except Exception:
        return
    n = len(children); i = 0
    while i < n:
        try:
            el = children[i]
            tag = (el.tag_name or "").lower()
            if tag in ("h2", "h3"):
                title = (el.text or "").strip() or f"섹션 {i+1}"
                j = i + 1
                while j < n:
                    try:
                        nxt = children[j]
                        if (nxt.tag_name or "").lower() in ("h2","h3"): break
                        j += 1
                    except Exception:
                        j += 1; continue
                scope_nodes = children[i+1:j]
                yield title, scope_nodes
                i = j
            else:
                i += 1
        except Exception:
            i += 1
            continue
    if not any((c.tag_name or "").lower() in ("h2","h3") for c in children if c):
        yield "본문", children

def _find_in_nodes(nodes, css_selector):
    found = []
    for n in nodes:
        if not n: continue
        try:
            found.extend(n.find_elements(By.CSS_SELECTOR, css_selector))
        except Exception:
            continue
    return found

# ========= 본문/탭 수집 =========
def collect_page_text(article) -> str:
    parts = []
    try:
        for block_title, node_scope in _iter_section_scopes(article):
            tablists = []
            tablists.extend(_find_in_nodes(node_scope, '[role="tablist"]'))
            tablists.extend(_find_in_nodes(node_scope, ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))

            code_items = []
            for idx, tablist in enumerate(tablists, start=1):
                try:
                    tabs = []
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                    tabs = [t for t in tabs if t.is_displayed()]
                    if not tabs: continue

                    for t_i in range(len(tabs)):
                        tabs_now = []
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                        tabs_now = [t for t in tabs_now if t.is_displayed()]
                        if t_i >= len(tabs_now): break
                        tab = tabs_now[t_i]
                        lang = _tab_label(tab, f"Tab {idx}-{t_i+1}")
                        res = _collect_snippets_for_tab(tab, tablist, lang)
                        if res: code_items.extend(res)
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue

            if code_items:
                formatted = [f"언어: {lab}\n{_norm_text(txt)}" for (lab, txt, _is_code) in code_items]
                parts.append(f"## {block_title}\n=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(formatted))
    except Exception:
        pass

    content = "\n\n".join(parts).strip()
    # 빈약하면 전체 본문 폴백
    if len(content) < 100:
        try:
            fallback = driver.execute_script("return (arguments[0].innerText||'').trim()", article)
        except Exception:
            fallback = article.text or ""
        fb = _norm_text(fallback)
        if len(fb) > len(content):
            return "## Fallback (article.innerText)\n" + fb
    return content

# ========= 배너/동의 닫기 =========
def _dismiss_banners():
    try:
        driver.execute_script("""
for (const el of document.querySelectorAll('button,[role="button"]')) {
  const t=(el.innerText||'').toLowerCase();
  if(t.includes('accept')||t.includes('agree')||t.includes('동의')) { el.click(); break; }
}
        """)
    except Exception:
        pass

# ========= 로드/재시도 =========
def _load_and_collect_with_retry(url, retries=2):
    global driver, wait
    for attempt in range(retries + 1):
        try:
            print(f"  시도 {attempt + 1}/{retries + 1}: 페이지 로딩...", flush=True)
            driver.switch_to.default_content()
            driver.get(url)
            wait.until(lambda d: d.execute_script(
                "return document.querySelector('article,main,body') !== null;"
            ))
            _dismiss_banners()
            # 핵심 노드 감지
            WebDriverWait(driver, 6).until(
                lambda d: d.execute_script(
                    "return !!document.querySelector('a, pre, code, devsite-code, .devsite-article-body');"
                )
            )
            try:
                article = driver.find_element(By.CSS_SELECTOR, "article, main, body")
            except Exception:
                article = driver.find_element(By.TAG_NAME, "body")
            return collect_page_text(article)
        except (TimeoutException, StaleElementReferenceException) as e:
            print(f"  재시도 사유: {type(e).__name__}", flush=True)
            if attempt >= retries: raise
            try: driver.refresh()
            except Exception: pass
            time.sleep(0.5)
        except WebDriverException:
            if attempt >= retries: raise
            print("  드라이버 재시작…", flush=True)
            try: driver.quit()
            except Exception: pass
            driver = setup_driver()
            wait = WebDriverWait(driver, 12)
            time.sleep(0.3)
        except Exception as e:
            print(f"  일반 오류: {e}", flush=True)
            if attempt >= retries: raise
            time.sleep(0.3)

# ========= 메인 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited, discovered = set(), set(q)
    pages_crawled = 0
    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited: continue
        print(f"\n({pages_crawled+1}) 크롤링: {url}", flush=True)
        try:
            page_text = _load_and_collect_with_retry(url, retries=1)
            if not (page_text or "").strip():
                try:
                    art = driver.find_element(By.TAG_NAME, "article")
                except Exception:
                    art = driver.find_element(By.CSS_SELECTOR, "main, body")
                txt = driver.execute_script("return (arguments[0].innerText||'').trim()", art) or (art.text or "")
                if txt:
                    page_text = "## Fallback (article.innerText)\n" + _norm_text(txt)

            filepath = os.path.join(OUTPUT_DIR, url_to_safe_filename(url))
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {url}\n\n{page_text}")
            print(f"저장 완료: {filepath}", flush=True)

            visited.add(url); pages_crawled += 1

            next_links = []
            for norm in extract_all_page_links():
                if norm not in visited and norm not in discovered:
                    next_links.append(norm)
            if next_links:
                q.extend(next_links); discovered.update(next_links)
                print(f"  ↳ 새 링크 {len(next_links)}개 추가 (대기열 {len(q)})", flush=True)
        except Exception as e:
            print(f"페이지 처리 중 오류: {url} - {e}", flush=True)
        time.sleep(CRAWL_DELAY_SEC)
    print(f"\n✅ 완료: 총 {pages_crawled} 페이지 크롤링 (상한 {MAX_PAGES})", flush=True)

if __name__ == "__main__":
    try:
        crawl()
    finally:
        try: driver.quit()
        except Exception: pass
        print("브라우저 종료", flush=True)
