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
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException

# ========= 기본 설정 =========
BASE_URL = "https://developers.google.com"
OUTPUT_DIR = "people_docs_crawled3"
MAX_PAGES = 500
CRAWL_DELAY_SEC = 2  # 딜레이 증가

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
    key_norm = re.sub(r"[()\[\]\s\.\-–—·:+]+", "", key)  # ".NET (C#)" → "netc#"
    return LANGUAGE_ALIASES.get(key_norm, LANGUAGE_ALIASES.get(key, [key_norm or key]))


# ========= 준비 =========
os.makedirs(OUTPUT_DIR, exist_ok=True)


def setup_driver():
    """드라이버 설정"""
    chrome_options = Options()
    chrome_options.add_argument("--headless=new")
    chrome_options.add_argument("--log-level=3")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument("--disable-extensions")

    # 올바른 방식의 pageLoadStrategy 설정 (argument가 아니라 capability)
    chrome_options.page_load_strategy = 'eager'  # 'normal' 대신 'eager'로 조금 더 빨리 반환

    chrome_options.add_argument(
        "--user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )

    service = ChromeService()
    drv = webdriver.Chrome(service=service, options=chrome_options)

    # 타임아웃
    drv.set_page_load_timeout(60)
    drv.implicitly_wait(10)
    return drv


# 전역 드라이버/대기자
driver = setup_driver()
wait = WebDriverWait(driver, 30)


# ========= 유틸 =========
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


def url_to_safe_filename(url: str) -> str:
    try:
        path = url.split("?")[0].replace(BASE_URL, "")
        fname = re.sub(r'[/\\?%*:|"<>]', "_", path).strip("_") or "index"
        p = urlparse(url)
        if p.query:
            q = re.sub(r'[^A-Za-z0-9=&._-]', "_", p.query)
            if q:
                fname += f"__{q}"
        return fname + ".txt"
    except Exception:
        return f"page_{int(time.time())}.txt"


def extract_all_page_links() -> list:
    """링크 추출 시 예외 처리 강화"""
    try:
        anchors = driver.find_elements(By.CSS_SELECTOR, "a[href]")
        links = []
        for a in anchors:
            try:
                href = a.get_attribute("href")
                if href:
                    links.append(urljoin(driver.current_url, href))
            except StaleElementReferenceException:
                continue
            except Exception:
                continue
        return links
    except Exception as e:
        print(f"링크 추출 중 오류: {e}", flush=True)
        return []


def _retry_stale(fn, tries=3, sleep=0.5):
    for i in range(tries):
        try:
            return fn()
        except StaleElementReferenceException:
            if i == tries - 1:
                raise
            time.sleep(sleep)
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(sleep)


# ========= Shadow/코드 헬퍼 =========
def _tab_label(el, default_name):
    try:
        txt = (el.text or "").strip()
        if txt:
            return txt
        for k in ("aria-label", "data-lang", "data-code-lang", "data-language", "title"):
            v = el.get_attribute(k)
            if v:
                return v.strip()
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
        if not pid:
            return None
        # 1) 현재 selector 안에서만 찾기(중복 ID 회피)
        host = _closest_selector_host(tablist)
        if host:
            try:
                return host.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
            except Exception:
                pass
        # 2) 현재 탭리스트 '이후'에서만 찾기
        try:
            return tablist.find_element(By.XPATH, f"following::section[@role='tabpanel' and @id='{pid}'][1]")
        except Exception:
            pass
        # 3) 마지막 폴백(기사 전역)
        try:
            return driver.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
        except Exception:
            return None
    except Exception:
        return None


def _shadow_texts_from_hosts(host_elements, lang_like_list=None):
    texts = []
    for host in host_elements:
        try:
            t = driver.execute_script("""
const host = arguments[0], want = arguments[1] || [];
try {
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
} catch(e) { return ""; }
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
try {
    const sr = host.shadowRoot; if(!sr) return "";
    const nodes = sr.querySelectorAll('pre, code'); let out=[];
    nodes.forEach(n=>{ const txt=(n.innerText||n.textContent||"").trim(); if(txt) out.push(txt); });
    return out.join("\\n\\n");
} catch(e) { return ""; }
                """, host)
                if t and t.strip():
                    texts.append(t.strip())
            except Exception:
                continue
    return "\n\n".join(texts)


def _visible_code_in(container):
    if not container:
        return ""

    texts = []
    try:
        nodes = []
        if container.tag_name.lower() in ("pre", "code"):
            nodes = [container]
        else:
            try:
                nodes = container.find_elements(By.CSS_SELECTOR,
                                                "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code")
            except Exception:
                pass

        for n in nodes:
            try:
                txt = (n.text or "").strip()
                if txt:
                    texts.append(txt)
            except Exception:
                continue
    except Exception:
        pass

    try:
        hosts = []
        if container.tag_name.lower() in ("devsite-code", "devsite-snippet"):
            hosts.append(container)

        if container.tag_name.lower() not in ("pre", "code"):
            try:
                hosts.extend(container.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet"))
            except Exception:
                pass

        if hosts:
            sh = _shadow_texts_from_hosts(hosts)
            if sh:
                texts.append(sh)
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
            val = (n.get_attribute("syntax") or "").strip().lower()
            if txt and (not cand or val in cand):
                out.append(txt)
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
                texts = []
                for n in nodes:
                    try:
                        txt = (n.text or "").strip()
                        if txt:
                            texts.append(txt)
                    except Exception:
                        continue
                if texts:
                    return "\n\n".join(texts).strip()
            except Exception:
                continue

    t = _find_code_with_syntax_attr([x.lower() for x in cands])
    if t:
        return t

    try:
        hosts = driver.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            t = _shadow_texts_from_hosts(hosts, [x.lower() for x in cands])
            if t:
                return t
    except Exception:
        pass

    return ""


# ========= 전/후 스냅샷(diff) =========
def _force_visible(elem):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", elem)
        time.sleep(0.2)
    except Exception:
        pass


def _snapshot_visible_code_nodes(root):
    if not root:
        return []

    nodes = []
    try:
        dom_nodes = root.find_elements(By.CSS_SELECTOR,
                                       "pre, code, div.highlight pre, div.devsite-code pre, div.devsite-code code")
    except Exception:
        dom_nodes = []

    for n in dom_nodes:
        try:
            txt = (n.text or "").strip()
            if txt:
                nodes.append(("dom", txt))
        except Exception:
            continue

    try:
        hosts = root.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            t = _shadow_texts_from_hosts(hosts)
            if t:
                for part in [p for p in t.split("\n\n") if p.strip()]:
                    nodes.append(("shadow", part))
    except Exception:
        pass

    syn = _find_code_with_syntax_attr([], root=root)
    if syn:
        for part in [p for p in syn.split("\n\n") if p.strip()]:
            nodes.append(("syntax", part))

    return nodes


def _diff_new_texts(before_nodes, after_nodes):
    before = {txt for (_k, txt) in before_nodes}
    return [txt for (_k, txt) in after_nodes if txt and txt not in before]


# ========= 탭 수집(하위 탭/디프 포함) =========
def _collect_snippets_for_tab(tab, tablist, lang_label, article_root):
    results, seen = [], set()

    def _emit(label, text, is_code):
        t = (text or "").strip()
        if not t:
            return
        key = (label, t)
        if key in seen:
            return
        seen.add(key)
        results.append((label, t, is_code))

    try:
        before = _snapshot_visible_code_nodes(article_root)
        _force_visible(tab)
        driver.execute_script("arguments[0].click();", tab)

        # 탭 활성화 대기
        try:
            wait.until(lambda _: (tab.get_attribute("aria-selected") == "true") or
                                 tab.get_attribute("aria-selected") is None)
        except TimeoutException:
            pass

        time.sleep(0.3)

        after = _snapshot_visible_code_nodes(article_root)
        new_texts = _diff_new_texts(before, after)

        if new_texts:
            _emit(lang_label, "\n\n".join(new_texts), True)
            return results
    except Exception as e:
        print(f"탭 처리 중 오류: {e}", flush=True)

    # 패널 찾기 및 하위 탭 처리
    try:
        section_scope = _find_panel_for_tab(tab, tablist) or tablist

        # 하위 탭 찾기
        sublists = []
        try:
            sublists.extend(section_scope.find_elements(By.CSS_SELECTOR, '[role="tablist"]'))
            sublists.extend(section_scope.find_elements(By.CSS_SELECTOR,
                                                        ".devsite-tabs, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))
        except Exception:
            pass

        if sublists:
            for st in sublists:
                try:
                    subs = []
                    subs.extend(st.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    subs.extend(st.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                    subs = [x for x in subs if x.is_displayed()]
                except Exception:
                    subs = []

                for s in subs:
                    try:
                        sublab = _tab_label(s, "옵션")
                        before_p = _visible_code_in(section_scope)
                        _force_visible(s)
                        driver.execute_script("arguments[0].click();", s)
                        time.sleep(0.2)
                        code = _visible_code_in(section_scope)

                        if (not code) or code == before_p:
                            code = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=section_scope)

                        if code:
                            _emit(f"{lang_label} · {sublab}", code, True)
                        else:
                            txt = (section_scope.text or "").strip()
                            if txt:
                                _emit(f"{lang_label} · {sublab}", txt, False)
                    except Exception:
                        continue
        else:
            code = _visible_code_in(section_scope)
            if not code:
                code = _find_code_with_syntax_attr(_lang_candidates(lang_label), root=section_scope)

            if code:
                _emit(lang_label, code, True)
            else:
                glob = _visible_code_by_language(lang_label)
                if glob:
                    _emit(lang_label, glob, True)
                else:
                    txt = (section_scope.text or "").strip()
                    if txt:
                        _emit(lang_label, txt, False)
    except Exception as e:
        print(f"섹션 처리 중 오류: {e}", flush=True)

    return results


# ========= 섹션 분리: h2/h3 사이의 '직계 형제들'만 스코프 =========
def _iter_section_scopes(article):
    try:
        body = article.find_element(By.CSS_SELECTOR, ".devsite-article-body")
    except Exception:
        body = article

    try:
        children = body.find_elements(By.XPATH, "./*")
    except Exception:
        return

    n = len(children)
    i = 0

    while i < n:
        try:
            el = children[i]
            tag = (el.tag_name or "").lower()

            if tag in ("h2", "h3"):
                title = (el.text or "").strip() or f"섹션 {i + 1}"
                j = i + 1
                while j < n:
                    try:
                        nxt = children[j]
                        if (nxt.tag_name or "").lower() in ("h2", "h3"):
                            break
                        j += 1
                    except Exception:
                        j += 1
                        continue

                scope_nodes = children[i + 1:j]  # 이 헤딩 ~ 다음 헤딩 사이 형제들
                yield title, scope_nodes
                i = j
            else:
                i += 1
        except Exception:
            i += 1
            continue

    # 헤딩이 하나도 없으면 본문 전체
    if not any((c.tag_name or "").lower() in ("h2", "h3") for c in children if c):
        yield "본문", children


def _find_in_nodes(nodes, css_selector):
    found = []
    for n in nodes:
        if not n:
            continue
        try:
            found.extend(n.find_elements(By.CSS_SELECTOR, css_selector))
        except Exception:
            continue
    return found


# ========= 본문/탭 수집 =========
def collect_page_text(article) -> str:
    if not article:
        return ""

    parts = []

    try:
        for block_title, node_scope in _iter_section_scopes(article):
            block_parts = []

            # 이 섹션 범위 안에서만 탭리스트/코드 찾기
            tablists = []
            tablists.extend(_find_in_nodes(node_scope, '[role="tablist"]'))
            tablists.extend(_find_in_nodes(node_scope,
                                           ".devsite-tabs, .devsite-language-selector, .code-tabs, ul.devsite-tabs, div.devsite-tabs"))

            code_items = []
            for idx, tablist in enumerate(tablists, start=1):
                try:
                    tabs = []
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                    tabs.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                    tabs = [t for t in tabs if t.is_displayed()]
                    if not tabs:
                        continue

                    # article 루트(디프용)
                    try:
                        article_root = driver.find_element(By.TAG_NAME, "article")
                    except Exception:
                        article_root = tablist

                    for t_i in range(len(tabs)):
                        try:
                            # 매번 재획득(anti-stale)
                            tabs_now = []
                            tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
                            tabs_now.extend(tablist.find_elements(By.CSS_SELECTOR, "button, a, li>button, li>a"))
                            tabs_now = [t for t in tabs_now if t.is_displayed()]

                            if t_i >= len(tabs_now):
                                break

                            tab = tabs_now[t_i]
                            lang = _tab_label(tab, f"Tab {idx}-{t_i + 1}")
                            res = _collect_snippets_for_tab(tab, tablist, lang, article_root)

                            if not res:
                                print(f"[tabs] '{lang}' 콘텐츠를 찾지 못함", flush=True)
                            else:
                                code_items.extend(res)
                        except StaleElementReferenceException:
                            continue
                        except Exception as e:
                            print(f"탭 처리 오류: {e}", flush=True)
                            continue
                except Exception as e:
                    print(f"탭리스트 처리 오류: {e}", flush=True)
                    continue

            if code_items:
                formatted = []
                for lab, txt, is_code in code_items:
                    prefix = "언어" if is_code else "언어 (코드 없음)"
                    formatted.append(f"{prefix}: {lab}\n{txt}")
                block_parts.append(f"## {block_title}\n=== 코드/텍스트 탭 수집 ===\n" + "\n\n".join(formatted))

            # 섹션 범위 일반 텍스트(탭/코드 제외)
            for n in node_scope:
                if not n:
                    continue
                try:
                    try:
                        nodes = n.find_elements(
                            By.CSS_SELECTOR,
                            ":scope:not([role='tablist']):not([role='tabpanel']):not(.devsite-code):not(.highlight)"
                        )
                    except Exception:
                        nodes = []

                    for node in nodes:
                        try:
                            txt = _retry_stale(lambda: (node.text or "").strip())
                            if txt:
                                block_parts.append(txt)
                        except StaleElementReferenceException:
                            continue
                        except Exception:
                            continue
                except Exception:
                    continue

            if block_parts:
                parts.append("\n\n".join(block_parts))
    except Exception as e:
        print(f"페이지 텍스트 수집 중 오류: {e}", flush=True)

    return "\n\n".join(filter(None, parts)).strip()


# ========= 배너/동의 닫기(있을 때만) =========
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
    global driver, wait  # 전역 갱신 중요
    for attempt in range(retries + 1):
        try:
            print(f"  시도 {attempt + 1}/{retries + 1}: 페이지 로딩...", flush=True)

            driver.switch_to.default_content()
            driver.get(url)

            # 문서 상태가 interactive/complete 가 될 때까지
            wait.until(lambda d: d.execute_script(
                "return ['interactive','complete'].includes(document.readyState)"
            ))
            time.sleep(0.5)

            # 컨테이너 확보
            try:
                article = driver.find_element(By.CSS_SELECTOR, "article, main, body")
            except Exception:
                article = driver.find_element(By.TAG_NAME, "body")

            # 배너 닫기(있다면)
            _dismiss_banners()

            # 핵심 노드가 하나라도 있으면 진행 (텍스트 채움 기다리지 않음)
            WebDriverWait(driver, 5).until(
                lambda d: d.execute_script(
                    "return !!document.querySelector('a, pre, code, devsite-code, .devsite-article-body')"
                )
            )

            print("  페이지 로드 완료, 텍스트 수집 중...", flush=True)
            return collect_page_text(article)

        except TimeoutException as e:
            print(f"  시도 {attempt + 1} 타임아웃: {e}", flush=True)
            if attempt >= retries:
                raise
            print("  페이지 새로고침 후 재시도...", flush=True)
            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(2)

        except StaleElementReferenceException as e:
            print(f"  시도 {attempt + 1} Stale 오류: {e}", flush=True)
            if attempt >= retries:
                raise
            print("  페이지 새로고침 후 재시도...", flush=True)
            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(2)

        except WebDriverException as e:
            print(f"  시도 {attempt + 1} WebDriver 오류: {e}", flush=True)
            if attempt >= retries:
                raise
            print("  드라이버 재시작 중...", flush=True)
            try:
                driver.quit()
            except Exception:
                pass
            driver = setup_driver()           # 전역 교체
            wait = WebDriverWait(driver, 30)  # 전역 교체
            time.sleep(1)

        except Exception as e:
            print(f"  시도 {attempt + 1} 일반 오류: {e}", flush=True)
            if attempt >= retries:
                raise
            time.sleep(1)


# ========= 메인 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited, discovered = set(), set(q)
    pages_crawled = 0
    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited:
            continue
        print(f"\n({pages_crawled+1}) 크롤링: {url}", flush=True)
        try:
            page_text = _load_and_collect_with_retry(url, retries=1)

            if not (page_text or "").strip():
                try:
                    art = driver.find_element(By.TAG_NAME, "article")
                except Exception:
                    art = driver.find_element(By.CSS_SELECTOR, "main, body")
                page_text = driver.execute_script("return (arguments[0].innerText||'').trim()", art) or (art.text or "")
                if page_text:
                    page_text = "## Fallback (article.innerText)\n" + page_text

            filepath = os.path.join(OUTPUT_DIR, url_to_safe_filename(url))
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {url}\n\n{page_text}")
            print(f"저장 완료: {filepath}", flush=True)

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
                print(f"  ↳ 새 링크 {len(next_links)}개 추가 (대기열 {len(q)})", flush=True)
        except Exception as e:
            print(f"페이지 처리 중 오류: {url} - {e}", flush=True)
        time.sleep(CRAWL_DELAY_SEC)
    print(f"\n✅ 완료: 총 {pages_crawled} 페이지 크롤링 (상한 {MAX_PAGES})", flush=True)


# 실행
if __name__ == "__main__":
    try:
        crawl()
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("브라우저 종료", flush=True)
