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
OUTPUT_DIR = "people_docs_fast_vis"
MAX_PAGES = 500
CRAWL_DELAY_SEC = 1  # 빠른 모드

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
    # 가장 빠른 제어권
    chrome_options.page_load_strategy = 'none'
    # 이미지 차단
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    # 안정적인 UA
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = ChromeService()
    drv = webdriver.Chrome(service=service, options=chrome_options)
    drv.set_page_load_timeout(45)
    drv.implicitly_wait(0)  # 암시적 대기 OFF
    return drv

driver = setup_driver()
wait = WebDriverWait(driver, 12)

# ========= 공통 정규화 =========
def _norm_text(t: str) -> str:
    if not t: return ""
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    return t.strip()

def _join_unique(arr):
    seen, out = set(), []
    for s in arr:
        n = _norm_text(s)
        if n and n not in seen:
            seen.add(n); out.append(n)
    return out

# ========= URL/링크 유틸 =========
def is_allowed_link(url: str) -> bool:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    try:
        p = urlparse(url)
        if p.netloc and p.netloc not in ALLOW_DOMAINS: return False
        path = p.path or ""
        if not any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES): return False
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

def _visible_codes_in(root):
    """
    root(=현재 탭 패널) 내부에서 '표시 중'인 코드 텍스트를 모두 추출.
    1) data-clipboard-text (복사 버튼) 우선
    2) pre[syntax], code[syntax]
    3) 열려있는 shadowRoot의 pre/code
    """
    try:
        arr = driver.execute_script("""
const root = arguments[0];
const out = []; const seen = new Set();

function vis(el){
  if(!el) return false;
  const st = getComputedStyle(el);
  if(st.display==='none' || st.visibility==='hidden' || +st.opacity===0) return false;
  const r = el.getBoundingClientRect();
  return r.width>0 && r.height>0;
}
function push(t){
  t=(t||"").trim(); if(!t) return;
  if(!seen.has(t)){ seen.add(t); out.push(t); }
}
// 1) 복사 버튼의 data-clipboard-text
root.querySelectorAll('button[data-clipboard-text], [data-clipboard-text]').forEach(b=>{
  if(!vis(b)) return;
  const t = b.getAttribute('data-clipboard-text');
  if(t) push(t);
});
// 2) light DOM 코드(문법 지정)
root.querySelectorAll('pre[syntax], code[syntax]').forEach(n=>{
  if(!vis(n)) return;
  const t=(n.innerText||n.textContent||"").trim();
  push(t);
});
// 3) shadow host 내부
root.querySelectorAll('devsite-code, devsite-snippet').forEach(host=>{
  if(!vis(host)) return;
  const sr = host.shadowRoot;
  if(!sr) return; // closed shadow는 접근 불가 → 위의 1)에서 대부분 해결됨
  sr.querySelectorAll('pre, code').forEach(n=>{
    if(!vis(n)) return;
    const t=(n.innerText||n.textContent||"").trim();
    push(t);
  });
});
// 4) 마지막 폴백: 일반 pre/code (눈에 보이는 것만)
if(out.length===0){
  root.querySelectorAll('pre, code').forEach(n=>{
    if(!vis(n)) return;
    const t=(n.innerText||n.textContent||"").trim();
    push(t);
  });
}

return out;
        """, root) or []
        return _join_unique(arr)
    except Exception:
        return []

def _panel_state(root):
    """탭 변경 감지를 위한 간단한 상태값(코드 개수 + 길이 합)"""
    try:
        return driver.execute_script("""
const r = arguments[0];
let count = 0, total = 0;
r.querySelectorAll('[data-clipboard-text]').forEach(b=>{
  const t=b.getAttribute('data-clipboard-text')||''; if(t){count++; total+=t.length;}
});
r.querySelectorAll('pre[syntax],code[syntax]').forEach(n=>{
  const t=(n.innerText||n.textContent||'').trim(); if(t){count++; total+=t.length;}
});
return count*1000000 + total;
        """, root) or 0
    except Exception:
        return 0

def _collect_snippets_for_tab(tab, tablist, lang_label):
    results = []
    snap_root = _find_panel_for_tab(tab, tablist) or tablist

    try:
        before_state = _panel_state(snap_root)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
        driver.execute_script("arguments[0].click();", tab)

        # 탭 활성화 또는 패널 상태 변화 대기
        try:
            WebDriverWait(driver, 6).until(
                lambda _: (tab.get_attribute("aria-selected") == "true") or
                          (tab.get_attribute("aria-selected") is None) or
                          (_panel_state(snap_root) != before_state)
            )
        except TimeoutException:
            pass

        # 표시 중 코드만 수집
        codes = _visible_codes_in(snap_root)
        if codes:
            for i, c in enumerate(codes, 1):
                results.append((f"{lang_label} · 셀#{i}", c, True))
            return results
    except Exception:
        pass

    # 폴백: 그래도 비면 패널의 전체 텍스트(가능하면 피함)
    try:
        fb = driver.execute_script("return (arguments[0].innerText||'').trim()", snap_root)
        fb = _norm_text(fb)
        if fb:
            results.append((f"{lang_label} · 셀#1", fb, True))
    except Exception:
        pass
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
                    "return !!document.querySelector('a, pre, code, devsite-code, .devsite-article-body, [data-clipboard-text]');"
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
