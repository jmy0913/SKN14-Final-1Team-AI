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
OUTPUT_DIR = "people_docs_crawled"
MAX_PAGES  = 500
CRAWL_DELAY_SEC = 1

# ========= 크롤 대상 제한 =========
ALLOW_DOMAINS = {"developers.google.com"}
ALLOW_PATH_PREFIXES = (
    "/people/api/rest",
    "/people/v1/",
    "/people/docs/",
    "/people/"
)
START_URLS = [
    "https://developers.google.com/people?hl=ko"
]

# ========= 언어 라벨 매핑/정규화 =========
LANGUAGE_ALIASES = {
    # ko 라벨
    "자바": ["java"],
    "파이썬": ["python", "py"],
    "프로토콜": ["http", "rest", "protocol"],
    "자바스크립트": ["javascript", "js", "node", "nodejs", "node.js"],
    # en/변형
    "java": ["java"],
    "python": ["python", "py"],
    "php": ["php"],
    "ruby": ["ruby"],
    "node.js": ["node", "nodejs", "node.js", "javascript", "js"],
    "nodejs": ["node", "nodejs", "node.js", "javascript", "js"],
    ".net": ["csharp", "dotnet", "cs", "c#"],
    "net": ["csharp", "dotnet", "cs", "c#"],
    "c#": ["csharp", "dotnet", "cs", "c#"],
    "dotnet": ["csharp", "dotnet", "cs", "c#"],
    "objc": ["objective-c", "objc"],
    "obj-c": ["objective-c", "objc"],
    "objective-c": ["objective-c", "objc"],
    "swift": ["swift"],
    "kotlin": ["kotlin"],
    "go": ["go", "golang"],
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
        qs = parse_qs(p.query)
        qs["hl"] = ["ko"]
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
        if q:
            fname += f"__{q}"
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
            if i == tries - 1:
                raise
            time.sleep(sleep)

# ========= Shadow DOM/코드 헬퍼 =========
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
    # 탭리스트 바로 뒤/부모 내 첫 코드/패널 후보
    try:
        sibs = tablist.find_elements(
            By.XPATH,
            "following-sibling::*[self::pre or self::code or contains(@class,'devsite-code') or contains(@class,'highlight') or @role='tabpanel' or name()='devsite-code' or name()='devsite-snippet'][position()<=6]"
        )
        if sibs:
            return sibs[0]
    except Exception:
        pass
    try:
        parent = tablist.find_element(By.XPATH, "./..")
        candidates = parent.find_elements(
            By.XPATH,
            ".//following::*[self::pre or self::code or contains(@class,'devsite-code') or contains(@class,'highlight') or @role='tabpanel' or name()='devsite-code' or name()='devsite-snippet'][position()<=10]"
        )
        if candidates:
            return candidates[0]
    except Exception:
        pass
    return _nearest_section_container(tablist)

def _shadow_texts_from_hosts(host_elements, lang_like_list=None):
    texts = []
    for host in host_elements:
        try:
            t = driver.execute_script("""
const host = arguments[0];
const want = arguments[1] || [];
const sr = host.shadowRoot;
if (!sr) return "";
const nodes = sr.querySelectorAll('pre, code, pre[class], code[class]');
let parts = [];
nodes.forEach(n => {
  const txt = (n.innerText || n.textContent || "").trim();
  if (!txt) return;
  if (want.length === 0) { parts.push(txt); return; }
  const cls = (n.getAttribute('class') || "").toLowerCase();
  const langAttr = (n.getAttribute('data-language') || n.getAttribute('data-code-lang') || n.getAttribute('data-lang') || "").toLowerCase();
  const hay = cls + " " + langAttr;
  for (const w of want) {
    if (w && hay.indexOf(w) !== -1) { parts.push(txt); return; }
  }
});
return parts.join("\\n\\n");
            """, host, lang_like_list or [])
            if t and t.strip():
                texts.append(t.strip())
        except Exception:
            continue
    if not texts and host_elements:
        for host in host_elements:
            try:
                t = driver.execute_script("""
const host = arguments[0];
const sr = host.shadowRoot;
if (!sr) return "";
const nodes = sr.querySelectorAll('pre, code');
let parts = [];
nodes.forEach(n => {
  const txt = (n.innerText || n.textContent || "").trim();
  if (txt) parts.push(txt);
});
return parts.join("\\n\\n");
                """, host)
                if t and t.strip():
                    texts.append(t.strip())
            except Exception:
                continue
    return "\n\n".join(texts)

def _visible_code_in(container):
    texts = []
    try:
        if container.tag_name.lower() in ("pre", "code"):
            candidates = [container]
        else:
            candidates = container.find_elements(By.CSS_SELECTOR, "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code")
        texts.extend([c.text for c in candidates if (c.text or "").strip()])
    except Exception:
        pass
    try:
        hosts = []
        if container.tag_name.lower() in ("devsite-code", "devsite-snippet"):
            hosts.append(container)
        if container.tag_name.lower() not in ("pre", "code"):
            hosts.extend(container.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet"))
        shadow_txt = _shadow_texts_from_hosts(hosts)
        if shadow_txt:
            texts.append(shadow_txt)
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
            txt = (n.text or "").strip()
            if not txt:
                continue
            val = (n.get_attribute("syntax") or "").strip().lower()
            if val and (val in cand):
                out.append(txt)
        except Exception:
            continue
    return "\n\n".join(out).strip()

def _visible_code_by_language(label):
    candidates = _lang_candidates(label)
    # data-* / class 기반
    for cand in candidates:
        for sel in (
            f'[data-language="{cand}"] pre',
            f'[data-language="{cand}"] code',
            f'[data-code-lang="{cand}"] pre',
            f'[data-code-lang="{cand}"] code',
            f'[data-lang="{cand}"] pre',
            f'[data-lang="{cand}"] code',
            f'.language-{cand} pre',
            f'.language-{cand} code',
            f'.devsite-syntax-{cand} pre',
            f'.devsite-syntax-{cand} code',
        ):
            try:
                nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                texts = [(n.text or "").strip() for n in nodes if (n.text or "").strip()]
                if texts:
                    return "\n\n".join(texts).strip()
            except Exception:
                continue
    # pre[syntax] / code[syntax]
    t = _find_code_with_syntax_attr([c.lower() for c in candidates])
    if t:
        return t
    # Shadow DOM
    try:
        hosts = driver.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            t = _shadow_texts_from_hosts(hosts, lang_like_list=[c.lower() for c in candidates])
            if t:
                return t
    except Exception:
        pass
    return ""

# ========= 스냅샷/디프(탭 클릭 전후 비교) =========
def _force_visible(elem):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        time.sleep(0.12)
        driver.execute_script("window.scrollBy(0, -60);")
        time.sleep(0.05)
        driver.execute_script("window.scrollBy(0, 60);")
        time.sleep(0.05)
    except Exception:
        pass

def _snapshot_visible_code_nodes(root):
    nodes = []
    try:
        dom_nodes = root.find_elements(By.CSS_SELECTOR, "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code")
    except Exception:
        dom_nodes = []
    for n in dom_nodes:
        try:
            txt = (n.text or "").strip()
            if txt:
                nodes.append(("dom", txt))
        except Exception:
            continue
    # Shadow DOM
    try:
        hosts = root.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            t = _shadow_texts_from_hosts(hosts)
            if t:
                for part in t.split("\n\n"):
                    if part.strip():
                        nodes.append(("shadow", part.strip()))
    except Exception:
        pass
    # syntax 속성 코드
    syn = _find_code_with_syntax_attr([], root=root)  # candidates 빈값이면 전부
    if syn:
        for part in syn.split("\n\n"):
            if part.strip():
                nodes.append(("syntax", part.strip()))
    return nodes

def _diff_new_texts(before_nodes, after_nodes):
    before = {txt for (_k, txt) in before_nodes}
    return [txt for (_k, txt) in after_nodes if txt and txt not in before]

# ========= 하위 탭까지 포함한 탭 수집 (article 디프 포함) =========
def _collect_snippets_for_tab(tab, tablist, lang_label, article_root):
    results = []
    seen = set()

    def _emit(label_str, text_str, is_code):
        t = (text_str or "").strip()
        if not t:
            return
        key = (label_str, t)
        if key in seen:
            return
        seen.add(key)
        results.append((label_str, t, is_code))

    # 클릭 전 article 스냅샷
    before_nodes = _snapshot_visible_code_nodes(article_root)

    # 클릭
    _force_visible(tab)
    driver.execute_script("arguments[0].click();", tab)
    try:
        def selected(_):
            val = tab.get_attribute("aria-selected")
            return (val == "true") if val is not None else True
        wait.until(selected)
    except TimeoutException:
        pass
    time.sleep(0.1)

    # 클릭 후 article 스냅샷
    after_nodes = _snapshot_visible_code_nodes(article_root)
    new_texts = _diff_new_texts(before_nodes, after_nodes)

    # 1) 전후 디프로 새로 나타난 코드가 있으면 우선 사용
    if new_texts:
        _emit(lang_label, "\n\n".join(new_texts), True)
        return results

    # 2) 탭 연계 패널/섹션에서 시도
    section_scope = _associated_code_region(tablist)
    panel = None
    panel_id = tab.get_attribute("aria-controls")
    if panel_id:
        try:
            panel = _retry_stale(lambda: driver.find_element(By.CSS_SELECTOR, f'#{panel_id}'))
        except Exception:
            panel = None
    if panel is None:
        panel = section_scope

    # 패널 내부 하위 탭들
    sub_tablists = []
    try:
        sub_tablists.extend(panel.find_elements(By.CSS_SELECTOR, '[role="tablist"]'))
        sub_tablists.extend(panel.find_elements(By.CSS_SELECTOR, ".devsite-tabs, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))
    except Exception:
        pass

    if sub_tablists:
        for stl in sub_tablists:
            try:
                sub_tabs = []
                sub_tabs.extend(stl.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                sub_tabs.extend(stl.find_elements(By.CSS_SELECTOR, "button, a, li > button, li > a"))
                sub_tabs = [s for s in sub_tabs if s.is_displayed()]
            except Exception:
                sub_tabs = []

            for s in sub_tabs:
                sub_label = _tab_label(s, "옵션")
                before = _visible_code_in(panel)
                _force_visible(s)
                driver.execute_script("arguments[0].click();", s)
                time.sleep(0.08)

                code_txt = _visible_code_in(panel)
                if (not code_txt) or code_txt == before:
                    code_txt = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=panel)
                if code_txt:
                    _emit(f"{lang_label} · {sub_label}", code_txt, True)
                else:
                    txt = (panel.text or "").strip()
                    if txt:
                        _emit(f"{lang_label} · {sub_label}", txt, False)
    else:
        # 3) 패널/섹션에서 코드 → syntax → 텍스트
        code_blocks = _visible_code_in(panel)
        if not code_blocks:
            code_blocks = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=panel)
        if code_blocks:
            _emit(lang_label, code_blocks, True)
        else:
            # 4) 기사 전역에서 라벨 후보로 검색
            glob = _visible_code_by_language(lang_label)
            if glob:
                _emit(lang_label, glob, True)
            else:
                txt = (panel.text or "").strip()
                if txt:
                    _emit(lang_label, txt, False)

    return results

# ========= 본문/탭 수집(헤딩 블록 단위) =========
def collect_page_text(article_element) -> str:
    parts = []
    headings = article_element.find_elements(By.CSS_SELECTOR, "h2, h3")
    blocks = []
    if headings:
        for i, h in enumerate(headings):
            title = (h.text or "").strip() or f"섹션 {i+1}"
            block_container = h.find_element(By.XPATH, "./..")
            blocks.append((title, block_container))
    else:
        blocks.append(("본문", article_element))

    for block_title, block_container in blocks:
        block_parts = []

        # 탭 컨테이너
        tab_containers = []
        tab_containers.extend(block_container.find_elements(By.CSS_SELECTOR, '[role="tablist"]'))
        tab_containers.extend(block_container.find_elements(
            By.CSS_SELECTOR, ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"
        ))
        uniq_tab_containers, seen_ids = [], set()
        for c in tab_containers:
            cid = getattr(c, "_id", id(c))
            if cid in seen_ids:
                continue
            seen_ids.add(cid)
            uniq_tab_containers.append(c)

        code_pairs_all = []
        for idx, tablist in enumerate(uniq_tab_containers, start=1):
            try:
                tabs = []
                tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li > button, li > a"))
                tabs = [t for t in tabs if t.is_displayed()]
                if not tabs:
                    continue

                # article 루트
                try:
                    article_root = driver.find_element(By.TAG_NAME, "article")
                except Exception:
                    article_root = block_container

                for t_i in range(len(tabs)):
                    try:
                        tabs_now = []
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                        tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li > button, li > a"))
                        tabs_now = [t for t in tabs_now if t.is_displayed()]
                        tab = tabs_now[t_i]
                    except Exception:
                        continue

                    lang_name = _tab_label(tab, f"Tab {idx}-{t_i+1}")
                    results = _collect_snippets_for_tab(tab, tablist, lang_name, article_root)

                    if not results:
                        print(f"[tabs] '{lang_name}' 콘텐츠를 찾지 못함")
                    else:
                        for lab, txt, is_code in results:
                            code_pairs_all.append((lab, txt, is_code))

            except StaleElementReferenceException:
                continue

        if code_pairs_all:
            formatted = []
            for lab, code_or_text, is_code in code_pairs_all:
                if is_code:
                    formatted.append(f"언어: {lab}\n{code_or_text}")
                else:
                    formatted.append(f"언어: {lab} (코드 없음)\n{code_or_text}")
            block_parts.append(f"## {block_title}\n=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(formatted))

        # 탭 이외 일반 텍스트(과다 중복 방지용 간단 수집)
        try:
            nodes = block_container.find_elements(
                By.CSS_SELECTOR,
                ":scope > :not(devsite-selector):not([role='tablist']):not([role='tabpanel']):not(.devsite-code):not(.highlight)"
            )
            for node in nodes:
                try:
                    txt = _retry_stale(lambda: (node.text or "").strip())
                    if txt:
                        block_parts.append(txt)
                except StaleElementReferenceException:
                    continue
        except Exception:
            pass

        if block_parts:
            parts.append("\n\n".join(block_parts))

    return "\n\n".join(filter(None, parts)).strip()

# ========= 페이지 로드+재시도 =========
def _load_and_collect_with_retry(url, retries=1):
    for attempt in range(retries + 1):
        try:
            driver.switch_to.default_content()
            driver.get(url)
            wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
            article = wait.until(EC.presence_of_element_located((By.TAG_NAME, "article")))
            wait.until(lambda d: (article.text or "").strip() != "")
            return collect_page_text(article)
        except StaleElementReferenceException:
            if attempt >= retries:
                raise
            print("[stale] 페이지 새로고침 후 재시도…")
            driver.refresh()
            time.sleep(0.3)

# ========= 크롤 메인 루프 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited, discovered = set(), set(q)
    pages_crawled = 0

    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited:
            continue

        print(f"\n({pages_crawled+1}) 크롤링: {url}")
        try:
            page_text = _load_and_collect_with_retry(url, retries=1)

            page_filename = url_to_safe_filename(url)
            filepath = os.path.join(OUTPUT_DIR, page_filename)
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {url}\n\n{page_text}")
            print(f"저장 완료: {filepath}")

            visited.add(url)
            pages_crawled += 1

            raw_links = extract_all_page_links()
            next_links = []
            for raw in raw_links:
                abs_url = urljoin(url, raw)
                norm = normalize_url(abs_url)
                if not is_allowed_link(norm):
                    continue
                if norm not in visited and norm not in discovered:
                    next_links.append(norm)

            if next_links:
                q.extend(next_links)
                discovered.update(next_links)
                print(f"  ↳ 새 링크 {len(next_links)}개 추가 (대기열 {len(q)})")

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
