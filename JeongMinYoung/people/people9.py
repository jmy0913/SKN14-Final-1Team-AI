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
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException

# ========= 기본 설정 =========
BASE_URL   = "https://developers.google.com"
OUTPUT_DIR = "people_docs_crawled1"
MAX_PAGES  = 500
CRAWL_DELAY_SEC = 1

# ========= 크롤 제한 =========
ALLOW_DOMAINS = {"developers.google.com"}
ALLOW_PATH_PREFIXES = (
    "/people/api/rest",
    "/people/v1/",
    "/people/docs/",
    "/people/",
)
START_URLS = ["https://developers.google.com/people?hl=ko"]  # 한국어만

# ========= 언어 라벨 매핑 =========
LANGUAGE_ALIASES = {
    "자바": ["java"], "파이썬": ["python","py"], "프로토콜": ["http","rest","protocol"],
    "자바스크립트": ["javascript","js","node","nodejs","node.js"],
    "java": ["java"], "python": ["python","py"], "php": ["php"], "ruby": ["ruby"],
    "node.js": ["node","nodejs","node.js","javascript","js"], "nodejs": ["node","nodejs","node.js","javascript","js"],
    ".net": ["csharp","dotnet","cs","c#"], "net": ["csharp","dotnet","cs","c#"], "c#": ["csharp","dotnet","cs","c#"], "dotnet": ["csharp","dotnet","cs","c#"],
    "objc": ["objective-c","objc"], "obj-c": ["objective-c","objc"], "objective-c": ["objective-c","objc"],
    "swift": ["swift"], "kotlin": ["kotlin"], "go": ["go","golang"],
}
def _lang_candidates(label: str):
    key = (label or "").strip().lower()
    key_norm = re.sub(r"[()\[\]\s\.\-–—·:+]+", "", key)  # ".NET (C#)" → "netc#"
    return LANGUAGE_ALIASES.get(key_norm, LANGUAGE_ALIASES.get(key, [key_norm or key]))

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
    if not url or url.startswith(("javascript:", "mailto:", "tel:")):
        return False
    p = urlparse(url)
    if p.netloc and p.netloc not in ALLOW_DOMAINS:
        return False
    path = p.path or ""
    if not any(path.startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
        return False
    qs = parse_qs(p.query)
    return qs.get("hl", [None])[0] == "ko"

def force_hl_ko(url: str) -> str:
    p = urlparse(url)
    if any((p.path or "").startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
        qs = parse_qs(p.query); qs["hl"] = ["ko"]
        return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
    return url

def normalize_url(url: str) -> str:
    url, _ = urldefrag(url)
    return force_hl_ko(url)

def url_to_safe_filename(url: str) -> str:
    path = url.split("?")[0].replace(BASE_URL, "")
    fname = re.sub(r'[/\\?%*:|"<>]', "_", path).strip("_") or "index"
    p = urlparse(url)
    if p.query:
        q = re.sub(r'[^A-Za-z0-9=&._-]', "_", p.query)
        if q: fname += f"__{q}"
    return fname + ".txt"

def extract_all_page_links() -> list:
    anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
    return [urljoin(driver.current_url, a.get_attribute("href"))
            for a in anchors if a.get_attribute("href")]

def _retry_stale(fn, tries=3, sleep=0.2):
    for i in range(tries):
        try:
            return fn()
        except StaleElementReferenceException:
            if i == tries - 1: raise
            time.sleep(sleep)

# ========= 공통 텍스트 추출기(비표시 포함) =========
def _text_or_content(el) -> str:
    """표시 여부와 무관하게 텍스트를 최대한 가져옴."""
    if el is None:
        return ""
    txt = (el.text or "").strip()
    if not txt:
        try:
            txt = (el.get_attribute("textContent") or "").strip()
        except Exception:
            pass
    return txt

# ========= Shadow/코드 헬퍼 =========
def _tab_label(el, default_name):
    txt = (el.text or "").strip()
    if txt: return txt
    for k in ("aria-label","data-lang","data-code-lang","data-language","title"):
        v = el.get_attribute(k)
        if v: return v.strip()
    return default_name

def _closest_selector_host(el):
    try:
        return el.find_element(By.XPATH, "ancestor::devsite-selector[1]")
    except Exception:
        return None

def _find_panel_for_tab(tab, tablist):
    pid = tab.get_attribute("aria-controls")
    if not pid: return None
    host = _closest_selector_host(tablist)
    if host:
        try: return host.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
        except Exception: pass
    try:
        return tablist.find_element(By.XPATH, f"following::section[@role='tabpanel' and @id='{pid}'][1]")
    except Exception:
        pass
    try:
        return driver.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
    except Exception:
        return None

def _shadow_texts_from_hosts(host_elements, lang_like_list=None):
    texts = []
    for host in host_elements:
        try:
            t = driver.execute_script("""
const host = arguments[0], want = arguments[1] || [];
const sr = host.shadowRoot; if (!sr) return "";
const nodes = sr.querySelectorAll('pre, code, pre[class], code[class]');
let parts = [];
nodes.forEach(n=>{
  const txt=(n.innerText||n.textContent||"").trim(); if(!txt) return;
  if(!want.length){ parts.push(txt); return; }
  const cls=(n.getAttribute('class')||"").toLowerCase();
  const lang=(n.getAttribute('data-language')||n.getAttribute('data-code-lang')||n.getAttribute('data-lang')||"").toLowerCase();
  const hay=cls+" "+lang;
  if(want.some(w=>w && hay.indexOf(w)!==-1)) parts.push(txt);
});
return parts.join("\\n\\n");
            """, host, lang_like_list or [])
            if t and t.strip(): texts.append(t.strip())
        except Exception:
            continue
    if not texts and host_elements:
        for host in host_elements:
            try:
                t = driver.execute_script("""
const host = arguments[0]; const sr = host.shadowRoot; if(!sr) return "";
const nodes = sr.querySelectorAll('pre, code'); let out=[];
nodes.forEach(n=>{ const txt=(n.innerText||n.textContent||"").trim(); if(txt) out.push(txt); });
return out.join("\\n\\n");
                """, host)
                if t and t.strip(): texts.append(t.strip())
            except Exception:
                continue
    return "\n\n".join(texts)

def _visible_code_in(container):
    texts = []
    try:
        nodes = [container] if container.tag_name.lower() in ("pre","code") else \
                container.find_elements(By.CSS_SELECTOR, "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code")
        for n in nodes:
            t = _text_or_content(n)
            if t: texts.append(t)
    except Exception:
        pass
    try:
        hosts = []
        if container.tag_name.lower() in ("devsite-code","devsite-snippet"): hosts.append(container)
        if container.tag_name.lower() not in ("pre","code"):
            hosts.extend(container.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet"))
        sh = _shadow_texts_from_hosts(hosts)
        if sh: texts.append(sh)
    except Exception:
        pass
    return "\n\n".join([t for t in texts if t]).strip()

def _find_code_with_syntax_attr(candidates, root=None):
    scope = root if root is not None else driver
    try:
        nodes = scope.find_elements(By.CSS_SELECTOR, "pre[syntax], code[syntax]")
    except Exception:
        nodes = []
    out = []
    cand = {c.lower() for c in candidates}
    for n in nodes:
        try:
            val = (n.get_attribute("syntax") or "").strip().lower()
            if (not cand) or (val in cand):
                t = _text_or_content(n)
                if t: out.append(t)
        except Exception:
            continue
    return "\n\n".join(out).strip()

def _visible_code_by_language(label):
    cands = _lang_candidates(label)
    for c in cands:
        for sel in (
            f'[data-language="{c}"] pre', f'[data-language="{c}"] code',
            f'[data-code-lang="{c}"] pre', f'[data-code-lang="{c}"] code',
            f'[data-lang="{c}"] pre', f'[data-lang="{c}"] code',
            f'.language-{c} pre', f'.language-{c} code',
            f'.devsite-syntax-{c} pre', f'.devsite-syntax-{c} code',
        ):
            try:
                nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                texts = [_text_or_content(n) for n in nodes]
                texts = [t for t in texts if t]
                if texts: return "\n\n".join(texts).strip()
            except Exception:
                continue
    t = _find_code_with_syntax_attr([x.lower() for x in cands])
    if t: return t
    try:
        hosts = driver.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            t = _shadow_texts_from_hosts(hosts, [x.lower() for x in cands])
            if t: return t
    except Exception:
        pass
    return ""

# ========= 탭 수집(패널 먼저 읽고, 비면 클릭해서 활성화 후 재시도) =========
def _wait_panel_active(panel):
    try:
        wait.until(lambda d: "devsite-active" in (panel.get_attribute("class") or ""))
    except TimeoutException:
        pass

def _click_tab(tab):
    # <tab> 내부 <a>가 실제 핸들러인 경우가 많음
    try:
        a = tab.find_element(By.CSS_SELECTOR, "a")
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", a)
        driver.execute_script("arguments[0].click();", a)
        return
    except Exception:
        pass
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
    driver.execute_script("arguments[0].click();", tab)

def _collect_snippets_for_tab(tab, tablist, lang_label, article_root):
    results, seen = [], set()
    def _emit(label, text, is_code):
        t = (text or "").strip()
        if not t: return
        key = (label, t)
        if key in seen: return
        seen.add(key); results.append((label, t, is_code))

    # 0) aria-controls 패널을 우선적으로 사용 (비표시여도 textContent로 강제 추출)
    panel = _find_panel_for_tab(tab, tablist)
    if panel is not None:
        # 패널 내부 pre/code/Shadow/syntax 모두 긁기
        txt = _visible_code_in(panel)
        if not txt:
            txt = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=panel)
        if not txt:
            # 그래도 없으면 패널 전체 텍스트(비표시 포함)
            txt = _text_or_content(panel)
        if txt:
            _emit(lang_label, txt, True)
            return results

    # 1) 클릭해서 활성화 → 해당 패널에서 재수집
    _click_tab(tab)
    try:
        wait.until(lambda _: (tab.get_attribute("aria-selected") == "true") or tab.get_attribute("aria-selected") is None)
    except TimeoutException:
        pass
    time.sleep(0.08)

    panel = panel or _find_panel_for_tab(tab, tablist)
    if panel is not None:
        _wait_panel_active(panel)
        txt = _visible_code_in(panel) or _find_code_with_syntax_attr(_lang_candidates(lang_label), root=panel)
        if not txt:
            txt = _text_or_content(panel)
        if txt:
            _emit(lang_label, txt, True)
            return results

    # 2) 섹션 스코프에서 보이는 코드
    scope = panel or tablist
    txt = _visible_code_in(scope)
    if not txt:
        txt = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=scope)
    if not txt:
        txt = _visible_code_by_language(lang_label)
    if txt:
        _emit(lang_label, txt, True)
    else:
        t = _text_or_content(scope)
        if t:
            _emit(lang_label, t, False)

    return results

# ========= 섹션 범위(헤딩 사이 형제들만) + 번호 매기기 =========
def _iter_section_scopes(article):
    try:
        body = article.find_element(By.CSS_SELECTOR, ".devsite-article-body")
    except Exception:
        body = article
    children = body.find_elements(By.XPATH, "./*")
    n = len(children)
    i = 0
    any_heading = False
    h2_idx, h3_idx = 0, 0
    while i < n:
        el = children[i]
        tag = (el.tag_name or "").lower()
        if tag in ("h2","h3"):
            any_heading = True
            title_raw = (el.text or "").strip() or f"섹션 {i+1}"
            if tag == "h2":
                h2_idx += 1; h3_idx = 0; num = f"{h2_idx}."
            else:
                h3_idx += 1; num = f"{h2_idx}.{h3_idx}."
            title = title_raw if re.match(r"^\d+(\.\d+)*\.\s", title_raw) else f"{num} {title_raw}"
            j = i + 1
            while j < n and (children[j].tag_name or "").lower() not in ("h2","h3"):
                j += 1
            yield title, children[i+1:j]
            i = j
        else:
            i += 1
    if not any_heading:
        yield "본문", children

def _find_in_nodes(nodes, css_selector):
    found = []
    for n in nodes:
        try:
            found.extend(n.find_elements(By.CSS_SELECTOR, css_selector))
        except Exception:
            continue
    return found

# ========= 본문/탭 수집 =========
def collect_page_text(article) -> str:
    parts = []
    # 페이지 h1
    try:
        h1 = article.find_element(By.TAG_NAME, "h1")
        page_title = (h1.text or "").strip()
        if page_title:
            parts.append(f"# {page_title}")
    except Exception:
        pass

    for block_title, node_scope in _iter_section_scopes(article):
        block_parts = [f"## {block_title}"]

        # 섹션 내 탭 수집
        tablists = []
        tablists.extend(_find_in_nodes(node_scope, '[role="tablist"]'))
        tablists.extend(_find_in_nodes(node_scope, ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))

        code_items = []
        for idx, tablist in enumerate(tablists, start=1):
            try:
                tabs = []
                tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a, tab"))
                tabs = [t for t in tabs if t.is_displayed()]
                if not tabs: continue

                # article 루트(일부 대기 로직에 사용 가능)
                try:
                    driver.find_element(By.TAG_NAME, "article")
                except Exception:
                    pass

                for t_i in range(len(tabs)):
                    tabs_now = []
                    tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a, tab"))
                    tabs_now = [t for t in tabs_now if t.is_displayed()]
                    if t_i >= len(tabs_now): break
                    tab = tabs_now[t_i]
                    lang = _tab_label(tab, f"Tab {idx}-{t_i+1}")
                    res = _collect_snippets_for_tab(tab, tablist, lang, None)
                    if not res:
                        print(f"[tabs] '{lang}' 콘텐츠를 찾지 못함")
                    else:
                        code_items.extend(res)
            except StaleElementReferenceException:
                continue

        if code_items:
            formatted = []
            for lab, txt, is_code in code_items:
                prefix = "언어" if is_code else "언어 (코드 없음)"
                formatted.append(f"{prefix}: {lab}\n{txt}")
            block_parts.append("=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(formatted))

        # 섹션 일반 텍스트 보강
        for node in node_scope:
            try:
                cls = (node.get_attribute("class") or "")
                role = (node.get_attribute("role") or "")
                if role not in ("tablist", "tabpanel") and "devsite-code" not in cls and "highlight" not in cls:
                    t0 = _text_or_content(node)
                    if t0:
                        block_parts.append(t0)
            except StaleElementReferenceException:
                continue

        parts.append("\n\n".join(block_parts))

    return "\n\n".join(filter(None, parts)).strip()

# ========= 로드/재시도 + innerText 대기 =========
def _load_and_collect_with_retry(url, retries=1):
    for attempt in range(retries + 1):
        try:
            driver.switch_to.default_content()
            driver.get(url)
            wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            wait.until(lambda d: d.execute_script(
                "const a=document.querySelector('article'); return a && (a.innerText||'').trim().length>0;"
            ))
            article = driver.find_element(By.TAG_NAME, "article")
            return collect_page_text(article)
        except StaleElementReferenceException:
            if attempt >= retries: raise
            print("[stale] 페이지 새로고침 후 재시도…")
            driver.refresh(); time.sleep(0.3)

# ========= 메인 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited, discovered = set(), set(q)
    pages_crawled = 0
    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited: continue
        print(f"\n({pages_crawled+1}) 크롤링: {url}")
        try:
            page_text = _load_and_collect_with_retry(url, retries=1)

            # 폴백: 비었으면 article.innerText 저장
            if not (page_text or "").strip():
                art = driver.find_element(By.TAG_NAME, "article")
                page_text = driver.execute_script("return (arguments[0].innerText||'').trim()", art) or (art.text or "")
                if page_text:
                    page_text = "## Fallback (article.innerText)\n" + page_text

            filepath = os.path.join(OUTPUT_DIR, url_to_safe_filename(url))
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {url}\n\n{page_text}")
            print(f"저장 완료: {filepath}")

            visited.add(url); pages_crawled += 1

            raw_links = extract_all_page_links()
            next_links = []
            for raw in raw_links:
                abs_url = urljoin(url, raw)
                norm = normalize_url(abs_url)
                if not is_allowed_link(norm): continue
                if norm not in visited and norm not in discovered:
                    next_links.append(norm)
            if next_links:
                q.extend(next_links); discovered.update(next_links)
                print(f"  ↳ 새 링크 {len(next_links)}개 추가 (대기열 {len(q)})")
        except Exception as e:
            print(f"페이지 처리 중 오류: {url} - {e}")
        time.sleep(CRAWL_DELAY_SEC)
    print(f"\n✅ 완료: 총 {pages_crawled} 페이지 크롤링 (상한 {MAX_PAGES})")

# 실행
try:
    crawl()
finally:
    driver.quit()
    print("브라우저 종료")
