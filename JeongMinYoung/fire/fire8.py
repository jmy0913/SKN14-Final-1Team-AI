# -*- coding: utf-8 -*-
import os
import re
import time
import random
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
OUTPUT_DIR = "firebase_firestore_fulltext_seq"
MAX_PAGES = 800
CRAWL_DELAY_SEC = 1

# ========= 크롤 제한 =========
# Firebase 제품 문서는 firebase.google.com/docs/**, Firestore는 cloud.google.com/firestore/docs/** 에 있음
ALLOW_DOMAINS = {"firebase.google.com", "cloud.google.com"}
ALLOW_PATH_PREFIXES = (
    "/docs/firestore",     # firebase.google.com/docs/firestore/**
    "/firestore/docs",     # cloud.google.com/firestore/docs/**
)
START_URLS = [
    "https://firebase.google.com/docs/firestore?hl=ko",
    "https://cloud.google.com/firestore/docs?hl=ko",
]

# ========= 수집 옵션 =========
# 탭/패널 내부의 설명 문장도 코드와 함께 담을지 여부
INCLUDE_PANEL_TEXT_IN_TABS = True
# 연산자/단일 토큰 등 허접 코드 필터링 강도
MIN_CODE_CHARS = 10  # 이 길이 미만의 단일 줄/기호 위주 코드는 버림 (표/탭 내부 제외)


# ========= 언어 라벨/토큰 =========
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
    "c++": ["c++", "cpp"], "unity": ["unity"], "dart": ["dart"],
    "terraform": ["terraform"], "gcloud": ["gcloud"], "firebase cli": ["firebase", "cli"],
}
def _lang_candidates(label: str):
    key = (label or "").strip().lower()
    key_norm = re.sub(r"[()\[\]\s\.\-–—·:+]+", "", key)
    return LANGUAGE_ALIASES.get(key_norm, LANGUAGE_ALIASES.get(key, [key_norm or key]))

TAB_LABEL_TOKENS = {
    "프로토콜","자바","java","python","py","php","ruby",
    "node","nodejs","node.js","javascript","js",
    "go","golang","kotlin","swift","objective-c","objc",
    "c#","dotnet",".net","net","maven","gradle","c++","unity",
    "dart","terraform","gcloud","firebase","cli"
}

# ========= 준비 =========
os.makedirs(OUTPUT_DIR, exist_ok=True)

def setup_driver():
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")   # 창 안 띄움
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")
    chrome_options.add_argument("--window-size=1366,900")
    chrome_options.page_load_strategy = 'eager'
    # 이미지 비활성화로 속도 ↑
    prefs = {"profile.managed_default_content_settings.images": 2}
    chrome_options.add_experimental_option("prefs", prefs)
    chrome_options.add_argument("--blink-settings=imagesEnabled=false")
    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = ChromeService()
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

# ---- 인라인 토큰 필터(짧은 <code> 토큰 제거)
INLINE_TOKEN_RE = re.compile(r'^[\w\.\-/#]+(?:\(\))?$')  # get, get(), a.b(), cities 등
ONLY_SYMBOLS_RE = re.compile(r"^[\s<>=+\-*/|&!?:~,.^%()\[\]{}\\;`']+$")

def _is_trivial_inline_code(t: str) -> bool:
    if not t:
        return True
    t = t.strip()
    if "\n" in t:
        # 여러 줄이면 길이만 체크
        return len(t) < MIN_CODE_CHARS and ONLY_SYMBOLS_RE.fullmatch(t)
    if ONLY_SYMBOLS_RE.fullmatch(t):
        return True
    if re.search(r'[;={}\[\]<>]|//|/\*|\b(import|class|def|await|return|var|let|const|final|new|func|package)\b', t):
        return False
    if INLINE_TOKEN_RE.fullmatch(t):
        return True
    if len(t) <= 18 and not re.search(r'\s', t):
        return True
    # 지나치게 짧은 한 줄 코드
    if len(t) < MIN_CODE_CHARS:
        return True
    return False

def _filter_code_candidates(arr):
    out = []
    for s in _join_unique(arr):
        if not s: continue
        if _looks_like_tab_labels(s): continue
        if _is_trivial_inline_code(s): continue
        out.append(s)
    return out

def is_allowed_link(url: str) -> bool:
    if not url or url.startswith(("javascript:", "mailto:", "tel:", "#")): return False
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
        path = url.split("?")[0].replace("https://cloud.google.com", "").replace("https://firebase.google.com", "")
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
    """패널 상태 스냅샷(길이 기반) — shadow DOM 포함"""
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
// shadow DOM 안쪽 코드까지 길이 합산
r.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  const sr=host.shadowRoot; if(!sr) return;
  sr.querySelectorAll('pre,code').forEach(n=>{
    const t=(n.innerText||n.textContent||'').trim(); if(t){count++; total+=t.length;}
  });
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
  if(n.closest('table')) return;
  if(n.closest('[role="tablist"],[role="tabpanel"],devsite-code,devsite-snippet')) return;
  if(!vis(n)) return;
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
        """, r) or []

    for getter in (_js_visible_clip, _js_visible_syntax, _js_visible_shadow, _js_visible_fallback):
        codes = _filter_code_candidates(getter(root))
        if codes:
            return codes
    return []

# ---- Overflow(더보기) 지원 ----
MORE_BTN_SEL = (
    "[aria-label*='더보기'],"                 # ko
    "[aria-label*='More'],"                   # en
    ".devsite-tabs__overflow, "
    ".devsite-overflow-menu__trigger, "
    "button[aria-haspopup='menu']"
)

def _find_more_button(tablist):
    for sel in MORE_BTN_SEL.split(","):
        sel = sel.strip()
        if not sel: continue
        try:
            btn = tablist.find_element(By.CSS_SELECTOR, sel)
            if btn and btn.is_displayed():
                return btn
        except Exception:
            continue
    return None

def _open_menu_and_collect_labels(more_btn):
    """더보기 메뉴를 열고, 스크롤하며 모든 항목 라벨을 수집"""
    labels = []
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'}); arguments[0].click();", more_btn)
        menu = WebDriverWait(driver, 4).until(
            lambda d: d.find_element(By.CSS_SELECTOR, "[role='menu'], .devsite-overflow-menu, .devsite-dropdown-menu")
        )
        labels = driver.execute_script("""
const menu=arguments[0];
const getLabel=el => (el.innerText||el.textContent||el.getAttribute('aria-label')||el.getAttribute('data-code-lang')||'').trim();
const out=[], seen=new Set();
menu.scrollTop = 0;
for (let i=0;i<50;i++){
  const items = Array.from(menu.querySelectorAll('[role=menuitem],a,button,li>button,li>a, tab'));
  for(const it of items){
    const lab = getLabel(it);
    if(lab && !seen.has(lab)){ seen.add(lab); out.push(lab); }
  }
  if (menu.scrollTop + menu.clientHeight >= menu.scrollHeight) break;
  menu.scrollTop = Math.min(menu.scrollTop + menu.clientHeight - 10, menu.scrollHeight);
}
return out;
        """, menu) or []
        # 메뉴 닫기 (밖 클릭)
        driver.execute_script("document.body.click();")
        time.sleep(0.05)
    except Exception:
        pass
    return _join_unique(labels)

def _activate_overflow_item(tablist, label):
    """더보기 메뉴에서 label과 일치하는 항목 클릭"""
    try:
        more = _find_more_button(tablist)
        if not more: return False
        driver.execute_script("arguments[0].click();", more)
        menu = WebDriverWait(driver, 4).until(
            lambda d: d.find_element(By.CSS_SELECTOR, "[role='menu'], .devsite-overflow-menu, .devsite-dropdown-menu")
        )
        item = None
        for it in menu.find_elements(By.CSS_SELECTOR, "[role=menuitem], a, button, li>button, li>a, tab"):
            try:
                lab = (it.text or "").strip() or (it.get_attribute("aria-label") or "").strip() \
                      or (it.get_attribute("data-code-lang") or "").strip()
                if lab and lab.strip().lower() == label.strip().lower():
                    item = it; break
            except Exception:
                continue
        if not item:
            driver.execute_script("document.body.click();")
            return False
        driver.execute_script("arguments[0].click();", item)
        time.sleep(0.05)
        return True
    except Exception:
        try: driver.execute_script("document.body.click();")
        except Exception: pass
        return False

def _panel_plain_texts(panel):
    if not panel:
        return []
    texts = driver.execute_script("""
const root=arguments[0], out=[];
root.querySelectorAll('p,li,dt,dd,blockquote').forEach(el=>{
  if (el.closest('pre,code,table,devsite-code,devsite-snippet')) return;
  const t=(el.innerText||el.textContent||'').trim();
  if (t) out.push(t);
});
return out;
    """, panel) or []
    cleaned = []
    for t in _join_unique(texts):
        if _looks_like_tab_labels(t):
            continue
        cleaned.append(t)
    return cleaned

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
        if INCLUDE_PANEL_TEXT_IN_TABS:
            ctx = _panel_plain_texts(snap_root)
            if ctx:
                results.append((f"{lang_label} · 설명", "\n\n".join(ctx), False))
        return results
    except Exception:
        return results

# ========= 섹션/표/텍스트 =========
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

def _extract_plain_texts(nodes):
    para = []
    TAGS = {"P","LI","DT","DD","BLOCKQUOTE"}

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
const root = arguments[0];
const out = [];
root.querySelectorAll('p,li,dt,dd,blockquote').forEach(el=>{
  if (el.closest('table')) return;
  if (el.closest('[role=\"tablist\"], [role=\"tabpanel\"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;
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

def _extract_tables(nodes):
    out_md = []
    def md_escape_cell(s: str) -> str:
        s = _norm_text(s)
        s = re.sub(r"\n+", " ", s)
        s = s.replace("|", "\\|")
        return s.strip()

    for n in nodes:
        try:
            tables = driver.execute_script("""
const root = arguments[0];
const out = [];
root.querySelectorAll('table').forEach(t=>{
  if (t.closest('[role=\"tablist\"], [role=\"tabpanel\"], devsite-code, devsite-snippet, pre, code, .devsite-toc')) return;

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
    if (rows.length) out.push({kind:"methods", rows});
    return;
  }

  const headers = [];
  const ths = t.querySelectorAll('thead th');
  if (ths.length) {
    ths.forEach(th => headers.push((th.innerText||th.textContent||'').trim()));
  } else {
    const firstTr = t.querySelector('tr');
    if (firstTr) {
      firstTr.querySelectorAll('th').forEach(th => headers.push((th.innerText||th.textContent||'').trim()));
    }
  }
  const rows = [];
  const trs = t.querySelectorAll('tbody tr, tr');
  trs.forEach(tr=>{
    const cells=[];
    tr.querySelectorAll('th,td').forEach(td=>{
      let x=(td.innerText||td.textContent||'').trim();
      cells.push(x);
    });
    if (cells.length) rows.push(cells);
  });
  out.push({kind:"generic", headers, rows});
});
return out;
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
                    for r in rows:
                        lines.append("| " + " | ".join(r) + " |")
                    out_md.append("\n".join(lines))
        except Exception:
            continue

    return _join_unique(out_md)

# ======= 언어 라벨 추정 (코드 내용/속성 기반 간단 추정) =======
def _guess_lang_label(code_text: str, default_label="코드") -> str:
    s = code_text.strip()
    # 가벼운 규칙들
    if re.search(r'^\s*(gcloud|bq|gsutil)\b', s, re.M): return "gcloud"
    if s.startswith("firebase "): return "Firebase CLI"
    if re.search(r'^\s*resource\s+"google_', s): return "Terraform"
    if "console.log(" in s or re.search(r'\b(new |const |let |=>)\b', s): return "웹"
    if re.search(r'^\s*import\s+\w+', s) and "package" not in s: return "파이썬"
    if re.search(r'^\s*def\s+\w+\(', s): return "파이썬"
    if re.search(r'^\s*class\s+\w+\s*{', s) and ";" in s: return "자바"
    if "DocumentReference" in s and ";" in s: return "자바"
    if re.search(r'^\s*val\s+\w+\s*=', s): return "Kotlin"
    if "#include" in s or "::" in s: return "C++"
    if "using System" in s or ".Collection(" in s and ";" in s and "new " in s: return ".NET"
    if re.search(r'^\s*func\s+\w+\(', s): return "Go"
    if "<?php" in s or "->" in s and "$" in s: return "PHP"
    if "print(" in s and ":" not in s and ";" not in s: return "파이썬"
    if re.search(r'^\s*final\s+\w+', s) and "Flutter" in s or "dart" in s.lower(): return "Dart"
    if "@objc" in s or "[[" in s: return "Objective-C"
    if "let " in s and ":" in s and "import Foundation" in s: return "Swift"
    return default_label

# ======= DOM 순서 스캐닝 =======
def _scan_blocks_ordered(container):
    """컨테이너 내부의 주요 블록(탭리스트/표/코드/iframe/텍스트)을 DOM 순서대로 식별"""
    prefix = f"sid{int(time.time()*1000)}_{random.randint(1000,9999)}"
    items = driver.execute_script("""
const root = arguments[0], prefix = arguments[1];
let idx = 0;
const mark = (el) => {
  let id = el.getAttribute('data-sid');
  if (!id) { id = prefix + '-' + (++idx); el.setAttribute('data-sid', id); }
  return id;
};
const out = [];
const TABSEL = '[role="tablist"], .devsite-tabs, .devsite-language-selector, ul.devsite-tabs, div.devsite-tabs';

const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT, null);
let node;
while ((node = walker.nextNode())) {
  const el = node;
  // 탭
  if (el.matches(TABSEL)) { out.push({kind:'tablist', sid:mark(el)}); el.setAttribute('data-sid-skip','1'); continue; }
  // 표
  if (el.matches('table')) { out.push({kind:'table', sid:mark(el)}); el.setAttribute('data-sid-skip','1'); continue; }
  // devsite-code host
  if (el.matches('devsite-code,devsite-snippet')) { out.push({kind:'devcode', sid:mark(el)}); el.setAttribute('data-sid-skip','1'); continue; }
  // 일반 pre (탭/표/devsite-code 안쪽 제외)
  if (el.matches('pre') && !el.closest('devsite-code,devsite-snippet') && !el.closest(TABSEL) && !el.closest('table')) {
    out.push({kind:'pre', sid:mark(el)}); el.setAttribute('data-sid-skip','1'); continue;
  }
  // 임베디드 iframe
  if (el.matches('devsite-iframe iframe, iframe.devsite-embedded')) { out.push({kind:'iframe', sid:mark(el)}); el.setAttribute('data-sid-skip','1'); continue; }
  // 텍스트 블록(인라인 코드/표/탭/코드 호스트 제외)
  if (el.matches('p,li,dt,dd,blockquote') &&
      !el.closest('table') && !el.closest(TABSEL) &&
      !el.closest('pre,code,devsite-code,devsite-snippet')) {
    out.push({kind:'text', sid:mark(el)});
  }
}
return out;
    """, container, prefix) or []
    return items

def _by_sid(sid):
    return driver.find_element(By.CSS_SELECTOR, f'[data-sid="{sid}"]')

# ======= 블록 단위 수집기 =======
def _collect_from_tablist(tablist):
    items, clicked = [], set()

    # 보이는 탭들
    try:
        tabs = []
        tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
        tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a, tab"))
        tabs = [t for t in tabs if t.is_displayed()]
        for i in range(len(tabs)):
            try:
                tabs_now = []
                tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a, tab"))
                tabs_now = [t for t in tabs_now if t.is_displayed()]
                if i >= len(tabs_now): break
                tab = tabs_now[i]
                lang = _tab_label(tab, f"Tab {i+1}")
                if lang in clicked: continue
                for lab, code, is_code in _collect_snippets_for_tab(tab, tablist, lang):
                    items.append((lab, code, is_code))
                clicked.add(lang)
            except Exception:
                continue
    except Exception:
        pass

    # 더보기(overflow)
    try:
        more = _find_more_button(tablist)
        if more:
            labels = _open_menu_and_collect_labels(more)
            for lab in labels:
                if lab in clicked: continue
                snap_root = tablist
                before = _panel_state(snap_root)
                if not _activate_overflow_item(tablist, lab):
                    continue
                try:
                    WebDriverWait(driver, 6).until(lambda _: _panel_state(snap_root) != before)
                except TimeoutException:
                    pass
                codes = _visible_codes_in(snap_root)
                if codes:
                    for i, c in enumerate(codes, 1):
                        items.append((f"{lab} · 셀#{i}", c, True))
                    clicked.add(lab)
                if INCLUDE_PANEL_TEXT_IN_TABS:
                    ctx = _panel_plain_texts(snap_root)
                    if ctx: items.append((f"{lab} · 설명", "\n\n".join(ctx), False))
    except Exception:
        pass

    return items

def _collect_codes_from_host(el):
    """devsite-code 호스트나 일반 pre 엘리먼트에서 코드만 추출"""
    blocks = driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); if(s.display==='none'||s.visibility==='hidden'||+s.opacity===0) return false;
               const b=el.getBoundingClientRect(); return b.width>0&&b.height>0;};
if(root.matches('devsite-code,devsite-snippet')){
  const sr=root.shadowRoot;
  if(sr){ sr.querySelectorAll('pre').forEach(n=>{ if(!vis(n)) return; const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t); }); }
  root.querySelectorAll('pre').forEach(n=>{ if(!vis(n)) return; const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t); });
}else if(root.matches('pre')){
  if(!root.closest('devsite-code,devsite-snippet') && !root.closest('[role="tablist"],[role="tabpanel"]')){
    const t=(root.innerText||root.textContent||'').trim(); if(t) out.push(t);
  }
}
return out;
    """, el) or []
    out=[]
    for i, txt in enumerate(_filter_code_candidates(blocks), 1):
        out.append((f"{_guess_lang_label(txt,'코드')} · 셀#{i}", _norm_text(txt), True))
    return out

def _collect_iframe_single(fr):
    items=[]
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", fr)
        src = fr.get_attribute("src") or ""
        driver.switch_to.frame(fr)
        codes = driver.execute_script("""
const out=[]; document.querySelectorAll('pre').forEach(n=>{
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
}); return out;
        """) or []
        if not codes:
            body_txt = driver.execute_script("return (document.body && document.body.innerText)||'';") or ""
            if _norm_text(body_txt): codes=[body_txt]
        driver.switch_to.default_content()
        for i, c in enumerate(_filter_code_candidates(codes), 1):
            label = "Maven" if "maven" in (src or "").lower() else ("Gradle" if "gradle" in (src or "").lower() else _guess_lang_label(c,"코드"))
            items.append((f"{label} · 셀#{i}", c, True))
    except Exception:
        try: driver.switch_to.default_content()
        except Exception: pass
    return items

def _table_to_md(table_el):
    md_list = _extract_tables([table_el])
    return md_list[0] if md_list else ""

# ========= 본문/탭 수집 (DOM 순서 보존) =========
def collect_page_text(article) -> str:
    parts = []
    try:
        for block_title, node_scope in _iter_section_scopes(article):
            section_out = []
            code_buffer=[]  # 연속 코드 블록을 한 번에 묶기 위함

            def flush_codes():
                nonlocal code_buffer, section_out
                if code_buffer:
                    lines = []
                    seen = set()
                    for (lab, txt, _is_code) in code_buffer:
                        key = (lab, _norm_text(txt))
                        if key in seen:
                            continue
                        seen.add(key)
                        # '코드' 라벨이면 내용 기반 추정 라벨로 교체
                        if lab.startswith("코드 · 셀#"):
                            lbl = _guess_lang_label(txt, "코드")
                            lab = lab.replace("코드", lbl, 1)
                        lines.append(f"언어: {lab}\n{_norm_text(txt)}")
                    if lines:
                        section_out.append("=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(lines))
                    code_buffer = []

            for node in node_scope:
                # 컨테이너 내 블록을 DOM 순서대로 스캔
                try:
                    blocks = _scan_blocks_ordered(node)
                except Exception:
                    blocks = []

                # 스캔 결과가 없으면(단순 텍스트만 있는 div 등) 일반 텍스트만 수집
                if not blocks:
                    txts = _extract_plain_texts([node])
                    if txts:
                        flush_codes()
                        section_out.append("\n\n".join(txts))
                    continue

                for b in blocks:
                    kind = b.get("kind"); sid = b.get("sid")
                    try:
                        el = _by_sid(sid)
                    except Exception:
                        continue

                    if kind == "text":
                        t = driver.execute_script("return (arguments[0].innerText||arguments[0].textContent||'').trim()", el) or ""
                        t = _norm_text(t)
                        if not t or _looks_like_tab_labels(t) or "도움이 되었나요" in t:
                            continue
                        flush_codes()
                        section_out.append(t)

                    elif kind == "table":
                        md = _table_to_md(el)
                        if md:
                            flush_codes()
                            section_out.append("### 표\n" + md)

                    elif kind in ("devcode","pre"):
                        code_buffer.extend(_collect_codes_from_host(el))

                    elif kind == "tablist":
                        code_buffer.extend(_collect_from_tablist(el))

                    elif kind == "iframe":
                        code_buffer.extend(_collect_iframe_single(el))

            # 섹션 끝에서 잔여 코드 버퍼 플러시
            flush_codes()

            if section_out:
                parts.append(f"## {block_title}\n" + "\n\n".join(section_out))

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
  const t=(el.innerText||'').toLowerCase();
  if(t.includes('accept')||t.includes('agree')||t.includes('동의')) { try{el.click();}catch(e){} }
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
            WebDriverWait(driver, 10).until(
                lambda d: d.execute_script("""
const b = document.querySelector('.devsite-article-body') || document.querySelector('article') || document.body;
if (!b) return false;
const t = (b.innerText||'').trim();
return t.length > 80;
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
