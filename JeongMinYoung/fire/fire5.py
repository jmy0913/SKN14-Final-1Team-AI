# -*- coding: utf-8 -*-
import os, re, time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse, urlencode, parse_qs

import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException

# ========= 기본 설정 (Firebase / Firestore 전용) =========
OUTPUT_DIR = "firebase_firestore_docs5"
MAX_PAGES = 500
CRAWL_DELAY_SEC = 1

# ========= 수집 허용 =========
ALLOW_DOMAINS = {"cloud.google.com", "firebase.google.com"}

# (사이트마다 허용 프리픽스)
PER_DOMAIN_PREFIXES = {
    "cloud.google.com": ("/firestore/",),          # https://cloud.google.com/firestore/...
    "firebase.google.com": ("/docs", "/docs/"),    # https://firebase.google.com/docs/...
}

START_URLS = [
    "https://cloud.google.com/firestore/docs?hl=ko",
    "https://firebase.google.com/docs?hl=ko",
]

# ========= 언어 라벨/토큰 =========
LANGUAGE_ALIASES = {
    "자바": ["java"], "파이썬": ["python","py"], "자바스크립트": ["javascript","js","node","nodejs","node.js"],
    "web": ["web","javascript","js"], "swift": ["swift"], "objective-c": ["objective-c","objc"],
    "kotlin": ["kotlin"], "java": ["java"], "dart": ["dart","flutter"],
    "python": ["python","py"], "node.js": ["node","nodejs","node.js","javascript","js"],
    "go": ["go","golang"], "php": ["php"], "c++": ["c++","cpp"], "unity": ["unity","c#","csharp"],
    "c#": ["c#","csharp","dotnet","cs"], "ruby": ["ruby"],
}
TAB_LABEL_TOKENS = {
    "프로토콜","자바","java","python","py","php","ruby","node","nodejs","node.js","javascript","js",
    "go","golang","kotlin","swift","objective-c","objc","c#","dotnet",".net","net","maven","gradle",
    "web","unity","c++","cpp","dart","flutter"
}

# ========= 브라우저 =========
def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1366,2000")
    chrome_options.page_load_strategy = 'eager'
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
    service = ChromeService()
    drv = webdriver.Chrome(service=service, options=chrome_options)
    drv.set_page_load_timeout(45)
    drv.implicitly_wait(0)
    return drv

driver = setup_driver()
wait = WebDriverWait(driver, 12)
os.makedirs(OUTPUT_DIR, exist_ok=True)

# ========= 유틸 =========
def _norm_text(t: str) -> str:
    if not t: return ""
    t = t.replace("\r\n","\n").replace("\r","\n")
    t = re.sub(r"[ \t]+\n","\n",t)
    t = re.sub(r"\n{3,}","\n\n",t)
    t = re.sub(r"[ \t]{2,}"," ",t)
    t = t.replace("\u200b","").replace("\ufeff","")
    t = re.sub(r"\s*wbr\s*", "", t, flags=re.I)
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

# 너무 짧거나 토큰만 있는 잡스니펫 제거 (단일 연산자/메서드명 등)
_SHORT_TOKEN = re.compile(r"^([<>]=?|==|!=|<=|>=|\w{1,10}\(\))$", re.I)
def _filter_code_candidates(arr):
    out = []
    for s in _join_unique(arr):
        if not s: continue
        if _looks_like_tab_labels(s): continue
        if len(s) < 3: continue
        if _SHORT_TOKEN.fullmatch(s.strip()): continue
        out.append(s)
    return out

def is_allowed_link(url: str) -> bool:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")): return False
    try:
        p = urlparse(url)
        if p.netloc and p.netloc not in ALLOW_DOMAINS: return False
        # 도메인별 프리픽스 체크
        allowed = PER_DOMAIN_PREFIXES.get(p.netloc, ())
        if allowed and not any((p.path or "").startswith(pref) for pref in allowed):
            return False
        qs = parse_qs(p.query)
        # 한국어 페이지만
        return qs.get("hl", [None])[0] == "ko"
    except Exception:
        return False

def force_hl_ko(url: str) -> str:
    try:
        p = urlparse(url); qs = parse_qs(p.query); qs["hl"] = ["ko"]
        return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
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
        p = urlparse(url)
        path = (p.path or "").strip("/") or "index"
        fname = re.sub(r'[/\\?%*:|"<>]', "_", path)
        if p.query:
            q = re.sub(r'[^A-Za-z0-9=&._-]', "_", p.query)
            if q: fname += f"__{q}"
        return fname + ".txt"
    except Exception:
        return f"page_{int(time.time())}.txt"

# ========= 스크롤(지연 로딩 깨우기) =========
def _progressive_scroll():
    try:
        total = driver.execute_script("return Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)||0;") or 0
        y, step = 0, 700
        while y < total + 1200:
            driver.execute_script("window.scrollTo(0, arguments[0]);", y)
            time.sleep(0.08)
            y += step
        # 맨 위로 복귀
        driver.execute_script("window.scrollTo(0, 0);")
        time.sleep(0.05)
    except Exception:
        pass

# ========= 링크 수집 =========
def extract_all_page_links() -> list:
    try:
        hrefs = driver.execute_script("""
const out = [];
(document.querySelector('article')||document.body)
  .querySelectorAll('a[href]')?.forEach(a=>{
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

# ========= 탭/코드 수집 도우미 =========
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
// also include devsite-code (light or shadow)
r.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  const sr=host.shadowRoot;
  const pick = (node)=>{(node.querySelectorAll('pre,code')||[]).forEach(n=>{
     const t=(n.innerText||n.textContent||'').trim(); if(t){count++; total+=t.length;}
  })};
  if(sr){ pick(sr); } else { pick(host); }
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

    # devsite-code / snippet (shadow + light DOM 모두)
    def _js_visible_devsite(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  if(!vis(host)) return;
  const collect = (node)=>{
    node.querySelectorAll('pre,code').forEach(n=>{
      if(!vis(n)) return;
      const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
    });
  };
  const sr = host.shadowRoot;
  if(sr) collect(sr); else collect(host);
});
return out;
        """, r) or []

    # 일반 pre/code (탭/표 내부는 제외)
    def _js_visible_fallback(r):
        return driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
root.querySelectorAll('pre,code').forEach(n=>{
  if(n.closest('table')) return;
  if(n.closest('[role="tablist"],[role="tabpanel"]')) return;
  if(!vis(n)) return;
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
        """, r) or []

    for getter in (_js_visible_clip, _js_visible_syntax, _js_visible_devsite, _js_visible_fallback):
        codes = _filter_code_candidates(getter(root))
        if codes:
            return codes
    return []

# overflow(더보기) 열어 모든 탭 요소 가져오기
def _open_overflow_and_get_items(tablist):
    try:
        elems = driver.execute_script("""
const list = arguments[0];
let btn = list.querySelector('.devsite-tabs-overflow-button');
if(!btn){
  // aria-label이 '더보기' or 'More'
  btn = Array.from(list.querySelectorAll('button[aria-haspopup="menu"]')).find(b=>{
    const t=(b.getAttribute('aria-label')||b.innerText||'').toLowerCase();
    return t.includes('더보기')||t.includes('more')||t.includes('옵션');
  });
}
if(!btn) return [];
// 열기
try{ btn.click(); }catch(e){}
const menuId = btn.getAttribute('aria-controls');
const menu = menuId ? document.getElementById(menuId) : list.querySelector('.devsite-tabs-overflow-menu,[role="menu"]');
if(!menu){ return []; }
menu.removeAttribute('hidden');
const items = Array.from(menu.querySelectorAll('tab[role="menuitem"],[role="menuitem"],a,button,li>a,li>button'));
return items;
        """, tablist) or []
        return elems
    except Exception:
        return []

def _collect_snippets_for_tab(tab, tablist, lang_label):
    results = []
    snap_root = _find_panel_for_tab(tab, tablist) or tablist
    try:
        before = _panel_state(snap_root)
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
        driver.execute_script("arguments[0].click();", tab)
        try:
            WebDriverWait(driver, 8).until(
                lambda _: (tab.get_attribute("aria-selected") == "true") or
                          (tab.get_attribute("aria-selected") is None) or
                          (_panel_state(snap_root) != before)
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

# ========= 섹션 나누기 =========
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
        yield "본문", children; return
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

# ========= 일반 텍스트 =========
def _extract_plain_texts(nodes):
    para, TAGS = [], {"P","LI","DT","DD","BLOCKQUOTE"}
    for n in nodes:
        try:
            tag = (n.tag_name or "").upper()
            if tag in TAGS:
                if driver.execute_script("return !!arguments[0].closest('table')", n):
                    pass
                else:
                    txt = driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trim()", n)
                    if txt: para.append(txt)
            texts = driver.execute_script("""
const root = arguments[0]; const out = [];
root.querySelectorAll('p,li,dt,dd,blockquote').forEach(el=>{
  if (el.closest('table')) return;
  if (el.closest('[role="tablist"], [role="tabpanel"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;
  const t = (el.innerText || el.textContent || '').trim(); if (t) out.push(t);
}); return out;
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

# ========= 표 =========
def _extract_tables(nodes):
    out_md = []
    def md_escape_cell(s: str) -> str:
        s = _norm_text(s); s = re.sub(r"\n+", " ", s); s = s.replace("|","\\|"); return s.strip()
    for n in nodes:
        try:
            tables = driver.execute_script("""
const root=arguments[0], out=[];
root.querySelectorAll('table').forEach(t=>{
  if (t.closest('[role="tablist"], [role="tabpanel"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;
  const isMethods = t.classList.contains('methods');
  if (isMethods) {
    const rows = [];
    t.querySelectorAll('tbody tr, tr').forEach(tr=>{
      const cells = Array.from(tr.children).filter(x=>/^(TD|TH)$/i.test(x.tagName));
      if (cells.length < 2) return;
      const left = (cells[0].innerText || cells[0].textContent || '').trim();
      const rightEl = cells[1];
      let path = "";
      const c = rightEl.querySelector('code, pre');
      if (c) path = (c.innerText || c.textContent || '').trim();
      let desc = (rightEl.innerText || rightEl.textContent || '').trim();
      if (path && desc.startsWith(path)) desc = desc.slice(path.length).trim();
      rows.push([left, path, desc]);
    });
    if (rows.length) out.push({kind:"methods", rows}); return;
  }
  const headers = []; const ths=t.querySelectorAll('thead th');
  if (ths.length) ths.forEach(th=>headers.push((th.innerText||th.textContent||'').trim()));
  else { const first=t.querySelector('tr'); if(first){ first.querySelectorAll('th').forEach(th=>headers.push((th.innerText||th.textContent||'').trim())); } }
  const rows=[]; t.querySelectorAll('tbody tr, tr').forEach(tr=>{
    const cells=[]; tr.querySelectorAll('th,td').forEach(td=>{ let x=(td.innerText||td.textContent||'').trim(); cells.push(x); });
    if (cells.length) rows.push(cells);
  });
  out.push({kind:"generic", headers, rows});
}); return out;
            """, n) or []
            for tbl in tables:
                kind = tbl.get("kind")
                if kind == "methods":
                    rows = tbl.get("rows") or []
                    if not rows: continue
                    header = ["방법","경로","설명"]
                    lines = ["| " + " | ".join(header) + " |",
                             "| " + " | ".join(["---"]*3) + " |"]
                    for r in rows:
                        cells = [md_escape_cell(x) for x in r]
                        if any(cells):
                            lines.append("| " + " | ".join((cells + [""]*3)[:3]) + " |")
                    out_md.append("\n".join(lines))
                else:
                    headers = [md_escape_cell(x) for x in (tbl.get("headers") or [])]
                    rows = [[md_escape_cell(c) for c in row] for row in (tbl.get("rows") or [])]
                    w = max([len(headers)] + [len(r) for r in rows]) if (headers or rows) else 0
                    if w == 0: continue
                    headers = (headers + [""]*w)[:w]
                    rows = [ (r + [""]*w)[:w] for r in rows ]
                    lines = ["| " + " | ".join(headers) + " |",
                             "| " + " | ".join(["---"]*w) + " |"]
                    for r in rows: lines.append("| " + " | ".join(r) + " |")
                    out_md.append("\n".join(lines))
        except Exception:
            continue
    return _join_unique(out_md)

# ========= 탭 밖 코드 =========
def _collect_loose_code(nodes):
    items = []
    for n in nodes:
        try:
            codes = driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
// devsite-code/shadow + light
root.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  const sr = host.shadowRoot;
  const collect=(node)=>node.querySelectorAll('pre,code').forEach(n=>{
    if(!vis(n)) return; const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
  });
  if(sr) collect(sr); else collect(host);
});
// 일반 pre/code (표 내부 제외, 탭 제외)
root.querySelectorAll('pre,code').forEach(n=>{
  if(n.closest('table')) return;
  if(n.closest('[role="tablist"],[role="tabpanel"],devsite-code,devsite-snippet')) return;
  if(!vis(n)) return;
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
            """, n) or []
            for i, c in enumerate(_filter_code_candidates(codes), 1):
                items.append((f"코드 · 셀#{i}", c, True))
        except Exception:
            continue
    return items

# ========= iframe 코드 =========
def _collect_iframe_codes(nodes):
    items = []
    frames = _find_in_nodes(nodes, "devsite-iframe iframe, iframe.devsite-embedded")
    for fr in frames:
        label = "iframe"
        try:
            driver.execute_script("arguments[0].scrollIntoView({block:'center'});", fr)
            src = fr.get_attribute("src") or ""
            driver.switch_to.frame(fr)
            codes = driver.execute_script("""
const out=[]; document.querySelectorAll('pre,code').forEach(n=>{
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
}); return out;
            """) or []
            if not codes:
                body_txt = driver.execute_script("return (document.body && document.body.innerText)||'';") or ""
                if _norm_text(body_txt):
                    codes = [body_txt]
            driver.switch_to.default_content()
            for i, c in enumerate(_filter_code_candidates(codes), 1):
                if "maven" in (src or "").lower(): label = "Maven"
                elif "gradle" in (src or "").lower(): label = "Gradle"
                items.append((f"{label} · 셀#{i}", c, True))
        except Exception:
            try: driver.switch_to.default_content()
            except Exception: pass
            try:
                src = fr.get_attribute("src") or ""
                if src:
                    r = requests.get(src, timeout=10)
                    if r.ok:
                        pres = re.findall(r"<pre[^>]*>(.*?)</pre>", r.text, flags=re.S|re.I)
                        txts = []
                        for html in pres:
                            t = re.sub(r"<[^>]+>", "", html)
                            t = _norm_text(t)
                            if t: txts.append(t)
                        if not txts:
                            t = _norm_text(re.sub(r"<[^>]+>", "", r.text))
                            if t: txts = [t]
                        for i, c in enumerate(_filter_code_candidates(txts), 1):
                            if "maven" in src.lower(): label = "Maven"
                            elif "gradle" in src.lower(): label = "Gradle"
                            items.append((f"{label} · 셀#{i}", c, True))
            except Exception:
                pass
            continue
    return items

# ========= 본문/탭 수집 =========
def collect_page_text(article) -> str:
    parts = []
    try:
        # 먼저 전체 스크롤로 lazy-load 깨우기
        _progressive_scroll()

        for block_title, node_scope in _iter_section_scopes(article):
            section_parts = []

            # (1) 일반 텍스트
            plain = _extract_plain_texts(node_scope)
            if plain:
                section_parts.append("\n\n".join(plain))

            # (1.5) 표
            tables_md = _extract_tables(node_scope)
            if tables_md:
                section_parts.append("### 표\n" + "\n\n".join(tables_md))

            # (2) 섹션 범위 안의 탭들 처리 (가시 + overflow)
            tablists = []
            tablists.extend(_find_in_nodes(node_scope, '[role="tablist"]'))
            tablists.extend(_find_in_nodes(node_scope, ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))

            code_items = []
            for idx, tablist in enumerate(tablists, start=1):
                try:
                    # 2-1. 현재 보이는 탭들
                    tabs = []
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                    tabs = [t for t in tabs if t.is_displayed()]
                    for t_i in range(len(tabs)):
                        # fresh query each time (DOM 변동)
                        tabs_now = []
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                        tabs_now = [t for t in tabs_now if t.is_displayed()]
                        if t_i >= len(tabs_now): break
                        tab = tabs_now[t_i]
                        lang = _tab_label(tab, f"Tab {idx}-{t_i+1}")
                        res = _collect_snippets_for_tab(tab, tablist, lang)
                        if res: code_items.extend(res)

                    # 2-2. overflow(더보기) 안의 탭들
                    overflow_items = _open_overflow_and_get_items(tablist)
                    for j, itm in enumerate(overflow_items, start=1):
                        lang = _tab_label(itm, f"Overflow {idx}-{j}")
                        res = _collect_snippets_for_tab(itm, tablist, lang)
                        if res: code_items.extend(res)
                except StaleElementReferenceException:
                    continue
                except Exception:
                    continue

            # (3) 탭 밖 코드
            loose = _collect_loose_code(node_scope)
            if loose: code_items.extend(loose)

            # (4) iframe 내부 코드
            iframe_codes = _collect_iframe_codes(node_scope)
            if iframe_codes: code_items.extend(iframe_codes)

            if code_items:
                seen = set(); fmt = []
                for (lab, txt, _is_code) in code_items:
                    key = (lab, _norm_text(txt))
                    if key in seen: continue
                    seen.add(key)
                    fmt.append(f"언어: {lab}\n{_norm_text(txt)}")
                section_parts.append("=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(fmt))

            if section_parts:
                parts.append(f"## {block_title}\n" + "\n\n".join(section_parts))
    except Exception:
        pass

    content = "\n\n".join(parts).strip()
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
  const t=(el.innerText||el.getAttribute('aria-label')||'').toLowerCase();
  if(t.includes('accept')||t.includes('agree')||t.includes('동의')||t.includes('허용')) { try{el.click();}catch(e){} }
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

            # 본문이 실제로 렌더될 때까지
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("""
const b = document.querySelector('.devsite-article-body') || document.querySelector('article') || document.body;
if (!b) return false; const t=(b.innerText||'').trim(); return t.length > 80;
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
            time.sleep(0.5)
        except Exception as e:
            print(f"  일반 오류: {e}", flush=True)
            if attempt >= retries: raise
            time.sleep(0.5)

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
