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
OUTPUT_DIR = "firebase_firestore_docs"
MAX_PAGES = 500
CRAWL_DELAY_SEC = 1  # 빠른 모드

# ========= 수집 허용 =========
ALLOW_DOMAINS = {"cloud.google.com", "firebase.google.com"}

# 공통 기본 프리픽스 (없어도 됨)
ALLOW_PATH_PREFIXES = ()

# 도메인별 허용 프리픽스
PER_DOMAIN_PREFIXES = {
    "cloud.google.com": ("/firestore/",),        # https://cloud.google.com/firestore/...
    "firebase.google.com": ("/docs", "/docs/"),  # https://firebase.google.com/docs/...
}

START_URLS = [
    "https://cloud.google.com/firestore/docs?hl=ko",
    "https://firebase.google.com/docs?hl=ko",
]

# ========= 언어 라벨 매핑/탭 라벨 토큰 =========
LANGUAGE_ALIASES = {
    "자바": ["java"], "파이썬": ["python", "py"], "프로토콜": ["http", "rest", "protocol"],
    "자바스크립트": ["javascript", "js", "node", "nodejs", "node.js"],
    "javascript": ["javascript", "js"], "java": ["java"], "python": ["python", "py"],
    "php": ["php"], "ruby": ["ruby"], "go": ["go", "golang"],
    "node.js": ["node", "nodejs", "node.js", "javascript", "js"],
    "nodejs": ["node", "nodejs", "node.js", "javascript", "js"],
    ".net": ["csharp", "dotnet", "cs", "c#"], "net": ["csharp", "dotnet", "cs", "c#"],
    "c#": ["csharp", "dotnet", "cs", "c#"], "dotnet": ["csharp", "dotnet", "cs", "c#"],
    "objc": ["objective-c", "objc"], "obj-c": ["objective-c", "objc"], "objective-c": ["objective-c", "objc"],
    "swift": ["swift"], "kotlin": ["kotlin"],
}
def _lang_candidates(label: str):
    key = (label or "").strip().lower()
    key_norm = re.sub(r"[()\[\]\s\.\-–—·:+]+", "", key)
    return LANGUAGE_ALIASES.get(key_norm, LANGUAGE_ALIASES.get(key, [key_norm or key]))

TAB_LABEL_TOKENS = {
    "프로토콜","자바","java","python","py","php","ruby",
    "node","nodejs","node.js","javascript","js",
    "go","golang","kotlin","swift","objective-c","objc",
    "c#","dotnet",".net","net","shell","bash","powershell","curl","http","rest"
}

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
    chrome_options.page_load_strategy = 'eager'
    # 이미지/폰트 비활성화로 속도↑
    prefs = {
        "profile.managed_default_content_settings.images": 2,
        "profile.managed_default_content_settings.fonts": 2,
    }
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
    service = ChromeService()  # Selenium Manager가 드라이버 자동 관리
    drv = webdriver.Chrome(service=service, options=chrome_options)
    drv.set_page_load_timeout(45)
    drv.implicitly_wait(0)
    return drv

driver = setup_driver()
wait = WebDriverWait(driver, 12)

# ========= 유틸 =========
def _norm_text(t: str) -> str:
    if not t: return ""
    t = t.replace("\r\n","\n").replace("\r","\n")
    t = re.sub(r"[ \t]+\n","\n",t)
    t = re.sub(r"\n{3,}","\n\n",t)
    t = re.sub(r"[ \t]{2,}"," ",t)
    return t.strip()

def _join_unique(arr):
    seen, out = set(), []
    for s in arr:
        n = _norm_text(s)
        if n and n not in seen:
            seen.add(n); out.append(n)
    return out

def _looks_like_tab_labels(text: str) -> bool:
    t = _norm_text(text)
    if not t: return False
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if 1 <= len(lines) <= 2:
        toks = []
        for ln in lines:
            toks += [w.lower() for w in re.split(r"[^\w\.\-\+#가-힣]+", ln) if w]
        if toks and all(w in TAB_LABEL_TOKENS for w in toks):
            return True
    toks = [w.lower() for w in re.split(r"[^\w\.\-\+#가-힣]+", t) if w]
    if toks and len(toks) <= 8 and all(w in TAB_LABEL_TOKENS for w in toks):
        return True
    if not re.search(r"[;={}\[\]()/<>]", t) and sum(w in TAB_LABEL_TOKENS for w in toks) >= 2:
        return True
    return False

def _filter_code_candidates(arr):
    return [s for s in _join_unique(arr) if s and not _looks_like_tab_labels(s)]

def _domain_prefixes(netloc: str):
    return PER_DOMAIN_PREFIXES.get(netloc, ALLOW_PATH_PREFIXES)

def force_hl_ko(url: str) -> str:
    try:
        p = urlparse(url)
        path = p.path or ""
        prefixes = _domain_prefixes(p.netloc)
        if prefixes and any(path.startswith(pref) for pref in prefixes):
            qs = parse_qs(p.query)
            qs["hl"] = ["ko"]
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

def is_allowed_link(url: str) -> bool:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")): return False
    try:
        p = urlparse(url)
        if not p.netloc or p.netloc not in ALLOW_DOMAINS:
            return False
        prefixes = _domain_prefixes(p.netloc)
        if prefixes and not any((p.path or "").startswith(pref) for pref in prefixes):
            return False
        # ko 강제 (없으면 normalize_url이 붙임)
        qs = parse_qs(p.query)
        return qs.get("hl", ["ko"])[0] == "ko"
    except Exception:
        return False

def url_to_safe_filename(url: str) -> str:
    try:
        p = urlparse(url)
        base = (p.netloc + (p.path or "/")).rstrip("/")
        fname = re.sub(r'[^A-Za-z0-9._/-]', "_", base).replace("/", "_")
        if p.query:
            q = re.sub(r'[^A-Za-z0-9=&._-]', "_", p.query)
            if q: fname += "__" + q
        return (fname or "index") + ".txt"
    except Exception:
        return f"page_{int(time.time())}.txt"

def extract_all_page_links() -> list:
    try:
        hrefs = driver.execute_script("""
const out = [];
(document.querySelectorAll('article a[href], main a[href], nav a[href]')||[]).forEach(a=>{
  const h=a.getAttribute('href'); if(h) out.push(h);
});
return out;
        """) or []
    except Exception:
        hrefs = []
    abs_links = [urljoin(driver.current_url, h) for h in hrefs]
    links = []
    for u in abs_links:
        nu = normalize_url(u)
        if is_allowed_link(nu):
            links.append(nu)
    return links

# ========= 탭/코드 수집 =========
def _tab_label(el, default_name):
    try:
        txt = (el.text or "").strip()
        if txt: return txt
        for k in ("aria-label","data-lang","data-code-lang","data-language","title"):
            v = el.get_attribute(k)
            if v: return v.strip()
    except Exception:
        pass
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
            try: return host.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
            except Exception: pass
        try: return tablist.find_element(By.XPATH, f"following::section[@role='tabpanel' and @id='{pid}'][1]")
        except Exception: pass
        try: return driver.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
        except Exception: return None
    except Exception:
        return None

def _panel_state(root):
    try:
        return driver.execute_script("""
const r = arguments[0];
let count=0,total=0;
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

def _visible_codes_in(root):
    def _js_visible_clip(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('[data-clipboard-text]').forEach(b=>{
  if(!vis(b)) return; const t=b.getAttribute('data-clipboard-text'); if(t) out.push(t);
});
return out;
        """, r) or []

    def _js_visible_syntax(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('pre[syntax],code[syntax]').forEach(n=>{
  if(!vis(n)) return; const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
        """, r) or []

    def _js_visible_shadow(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  if(!vis(host)) return; const sr=host.shadowRoot; if(!sr) return;
  sr.querySelectorAll('pre,code').forEach(n=>{
    if(!vis(n)) return; const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
  });
});
return out;
        """, r) or []

    def _js_visible_fallback(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('pre,code').forEach(n=>{
  if(!vis(n)) return;
  if(n.closest('devsite-code,devsite-snippet')) return;
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
        """, r) or []

    for getter in (_js_visible_clip, _js_visible_syntax, _js_visible_shadow, _js_visible_fallback):
        codes = _filter_code_candidates(getter(root))
        if codes:
            return codes
    return []

def _collect_snippets_for_tab(tab, tablist, lang_label):
    results = []
    snap_root = _find_panel_for_tab(tab, tablist) or tablist
    try:
        before_state = _panel_state(snap_root)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
        driver.execute_script("arguments[0].click();", tab)
        try:
            WebDriverWait(driver, 6).until(
                lambda _: (tab.get_attribute("aria-selected") == "true") or
                          (tab.get_attribute("aria-selected") is None) or
                          (_panel_state(snap_root) != before_state)
            )
        except TimeoutException:
            pass
        codes = _visible_codes_in(snap_root)
        if codes:
            for i, c in enumerate(codes, 1):
                results.append((f"{lang_label} · 셀#{i}", c, True))
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

    heads = []
    for idx, el in enumerate(children):
        try:
            if (el.tag_name or "").lower() in ("h2", "h3"): heads.append(idx)
        except Exception:
            continue

    if not heads:
        yield "본문", children
        return

    first = heads[0]
    if first > 0:
        yield "서문", children[0:first]

    for i, hidx in enumerate(heads):
        try:
            title = (children[hidx].text or "").strip() or f"섹션 {i+1}"
        except Exception:
            title = f"섹션 {i+1}"
        end = heads[i+1] if i + 1 < len(heads) else len(children)
        yield title, children[hidx+1:end]

def _find_in_nodes(nodes, css_selector):
    found = []
    for n in nodes:
        if not n: continue
        try:
            found.extend(n.find_elements(By.CSS_SELECTOR, css_selector))
        except Exception:
            continue
    return found

# ========= 표(테이블) 추출 =========
def _extract_tables(nodes):
    tables_md = []
    for n in nodes:
        try:
            tbls = driver.execute_script("""
const root=arguments[0];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
const list=[];
root.querySelectorAll('table').forEach(t=>{
  if(!vis(t)) return;
  if (t.closest('[role="tablist"], [role="tabpanel"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;
  const rows=[...t.querySelectorAll('tr')];
  const out=[];
  rows.forEach((r,i)=>{
    const cells=[...r.children].filter(x=>x.tagName==='TD'||x.tagName==='TH');
    if(!cells.length) return;
    out.push(cells.map(c=>(c.innerText||c.textContent||'').trim()));
  });
  if(out.length) list.push(out);
});
return list;
            """, n) or []
            for grid in tbls:
                if not grid: continue
                # 헤더 판단
                header = grid[0]
                has_th = any(bool(re.search(r"\S", c)) for c in header)
                if has_th:
                    md = []
                    md.append("| " + " | ".join(h if h else " " for h in header) + " |")
                    md.append("| " + " | ".join("---" for _ in header) + " |")
                    for row in grid[1:]:
                        md.append("| " + " | ".join(row) + " |")
                    tables_md.append("\n".join(md))
                else:
                    # 헤더 없는 표 → 전체를 그대로 나열
                    md = []
                    for row in grid:
                        md.append("| " + " | ".join(row) + " |")
                    tables_md.append("\n".join(md))
        except Exception:
            continue
    if tables_md:
        return "### 표\n" + "\n\n".join(tables_md)
    return ""

# ========= 일반 텍스트 추출 =========
def _extract_plain_texts(nodes):
    para = []
    TAGS = {"P","LI","DT","DD","BLOCKQUOTE"}

    for n in nodes:
        try:
            # 루트 자체가 대상 태그이면 포함
            tag = (n.tag_name or "").upper()
            if tag in TAGS:
                txt = driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trim()", n)
                if txt: para.append(txt)

            # 자식들에서 대상 태그 수집
            texts = driver.execute_script("""
const root = arguments[0];
const out = [];
root.querySelectorAll('p,li,dt,dd,blockquote').forEach(el=>{
  if (el.closest('[role="tablist"], [role="tabpanel"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;
  const t = (el.innerText || el.textContent || '').trim();
  if (t) out.push(t);
});
return out;
            """, n) or []
            para.extend(texts)
        except Exception:
            continue

    cleaned = []
    for t in _join_unique(para):
        low = t.lower()
        if ("도움이 되었나요" in t) or ("google developer" in low):
            continue
        if _looks_like_tab_labels(t):
            continue
        cleaned.append(t)
    return cleaned

# ========= 본문/탭/표 수집 =========
def collect_page_text(article) -> str:
    parts = []
    try:
        for block_title, node_scope in _iter_section_scopes(article):
            section_parts = []

            # (1) 일반 텍스트
            plain = _extract_plain_texts(node_scope)
            if plain:
                section_parts.append("\n\n".join(plain))

            # (2) 표 추출
            table_md = _extract_tables(node_scope)
            if table_md:
                section_parts.append(table_md)

            # (3) 섹션 범위 안의 탭들 처리
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
                section_parts.append("=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(formatted))

            if section_parts:
                parts.append(f"## {block_title}\n" + "\n\n".join(section_parts))
    except Exception:
        pass

    content = "\n\n".join(parts).strip()
    if len(content) < 100:
        try:
            fallback = driver.execute_script("return (arguments[0].innerText||'').trim()", article)
        except Exception:
            try:
                fallback_el = driver.find_element(By.CSS_SELECTOR, "main, body")
                fallback = driver.execute_script("return (arguments[0].innerText||'').trim()", fallback_el)
            except Exception:
                fallback = ""
        fb = _norm_text(fallback or "")
        if len(fb) > len(content):
            return "## Fallback (article.innerText)\n" + fb
    return content

# ========= 배너/동의 닫기 =========
def _dismiss_banners():
    try:
        driver.execute_script("""
for (const el of document.querySelectorAll('button,[role="button"]')) {
  const t=(el.innerText||'').toLowerCase();
  if(t.includes('accept')||t.includes('agree')||t.includes('동의')) { try{ el.click(); }catch(e){} }
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
                "return document.readyState==='interactive' || document.readyState==='complete';"
            ))
            _dismiss_banners()
            WebDriverWait(driver, 8).until(
                lambda d: d.execute_script("""
const b = document.querySelector('.devsite-article-body, article, main, body');
if (!b) return false;
const t = (b.innerText||'').trim();
return t.length > 120;
""")
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
            time.sleep(0.6)
        except WebDriverException:
            if attempt >= retries: raise
            print("  드라이버 재시작…", flush=True)
            try: driver.quit()
            except Exception: pass
            driver = setup_driver()
            wait = WebDriverWait(driver, 12)
            time.sleep(0.4)
        except Exception as e:
            print(f"  일반 오류: {e}", flush=True)
            if attempt >= retries: raise
            time.sleep(0.4)

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
