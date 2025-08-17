# -*- coding: utf-8 -*-
import os
import re
import time
from collections import deque
from urllib.parse import urljoin, urlparse, urldefrag, urlunparse, urlencode, parse_qs

import requests

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.common.exceptions import StaleElementReferenceException, TimeoutException, WebDriverException

# ========= 기본 설정 =========
OUTPUT_DIR = "firebase_firestore_sequential"
MAX_PAGES = 800
CRAWL_DELAY_SEC = 1

# ========= 크롤 제한 =========
ALLOW_DOMAINS = {"firebase.google.com", "cloud.google.com"}
ALLOW_PATH_PREFIXES = ("/docs/firestore", "/firestore/docs")
START_URLS = [
    "https://firebase.google.com/docs/firestore?hl=ko",
    "https://cloud.google.com/firestore/docs?hl=ko",
]

# ========= 언어 라벨/토큰 =========
LANGUAGE_ALIASES = {
    "자바": ["java"], "파이썬": ["python", "py"], "프로토콜": ["http", "rest", "protocol"],
    "자바스크립트": ["javascript", "js", "node", "nodejs", "node.js"],
    "java": ["java"], "python": ["python", "py"], "php": ["php"], "ruby": ["ruby"],
    "node.js": ["node", "nodejs", "node.js", "javascript", "js"],
    "nodejs": ["node", "nodejs", "node.js", "javascript", "js"],
    ".net": ["csharp", "dotnet", "cs", "c#"], "net": ["csharp", "dotnet", "cs", "c#"],
    "c#": ["csharp", "dotnet", "cs", "c#"],
    "dotnet": ["csharp", "dotnet", "cs", "c#"],
    "objc": ["objective-c", "objc"], "obj-c": ["objective-c", "objc"], "objective-c": ["objective-c", "objc"],
    "swift": ["swift"], "kotlin": ["kotlin"], "go": ["go", "golang"], "c++": ["c++", "cpp"], "unity": ["unity"],
}

TAB_LABEL_TOKENS = {
    "프로토콜", "자바", "java", "python", "py", "php", "ruby",
    "node", "nodejs", "node.js", "javascript", "js",
    "go", "golang", "kotlin", "swift", "objective-c", "objc",
    "c#", "dotnet", ".net", "net", "maven", "gradle", "c++", "unity"
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
    chrome_options.add_argument("--window-size=1366,900")
    chrome_options.page_load_strategy = 'eager'
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
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = t.replace("\u200b", "").replace("\ufeff", "")
    t = re.sub(r"\s*wbr\s*", "", t, flags=re.I)
    return t.strip()


def _join_unique(arr):
    seen, out = set(), []
    for s in arr:
        n = _norm_text(s)
        if n and n not in seen:
            seen.add(n);
            out.append(n)
    return out


def _looks_like_tab_labels(text: str) -> bool:
    t = _norm_text(text)
    if not t: return False
    lines = [x.strip() for x in t.splitlines() if x.strip()]
    if 1 <= len(lines) <= 2:
        toks = []
        for ln in lines:
            toks += [w.lower() for w in re.split(r"[^\w\.\-\+#가-힣]+", ln) if w]
        if toks and all(w in TAB_LABEL_TOKENS for w in toks): return True
    toks = [w.lower() for w in re.split(r"[^\w\.\-\+#가-힣]+", t) if w]
    if toks and len(toks) <= 8 and all(w in TAB_LABEL_TOKENS for w in toks): return True
    if not re.search(r"[;={}\[\]()/<>]", t) and sum(w in TAB_LABEL_TOKENS for w in toks) >= 2: return True
    return False


def _guess_lang_label(text: str, default_label: str = "코드") -> str:
    t = text.strip()
    if re.search(r'(?m)^\s*(\$ |gcloud |curl |kubectl |firebase |npm |yarn |export |set )', t): return "Bash"
    if re.search(r'(?i)\bSELECT\b.+\bFROM\b', t): return "SQL"
    if re.fullmatch(r'\s*\{[\s\S]*\}\s*', t) and ('"' in t or ':' in t): return "JSON"
    if re.search(r'(?m)^\s*\w+:\s', t) and not re.search(r'[{};]', t): return "YAML"
    if t.startswith("<") and re.search(r'</?[A-Za-z][^>]*>', t): return "HTML/XML"
    if re.search(r'(?m)^\s*(resource|provider)\s+"', t): return "HCL"
    return default_label or "코드"


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


def normalize_url(url: str) -> str:
    try:
        url, _ = urldefrag(url)
        p = urlparse(url)
        if any((p.path or "").startswith(prefix) for prefix in ALLOW_PATH_PREFIXES):
            qs = parse_qs(p.query);
            qs["hl"] = ["ko"]
            return urlunparse(p._replace(query=urlencode(qs, doseq=True)))
        return url
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


# ========= 탭/코드 수집 함수들 =========
def _tab_label(el, default_name):
    try:
        txt = (el.text or "").strip()
        if txt: return txt
        for k in ("aria-label", "data-lang", "data-code-lang", "data-language", "title"):
            v = el.get_attribute(k)
            if v: return v.strip()
    except Exception:
        pass
    return default_name


def _find_panel_for_tab(tab, tablist):
    """탭에 연결된 패널 찾기"""
    try:
        pid = tab.get_attribute("aria-controls")
        if pid:
            return driver.find_element(By.CSS_SELECTOR, f"section[role='tabpanel']#{pid}")
    except Exception:
        pass
    return tablist


def _visible_codes_in(root):
    """패널에서 보이는 코드들 추출"""
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", root)
    except Exception:
        pass

    try:
        codes = driver.execute_script("""
const root=arguments[0], out=[];
const vis=el=>{const s=getComputedStyle(el); return s.display!=='none'&&s.visibility!=='hidden'&&+s.opacity!==0;};

// data-clipboard-text
root.querySelectorAll('[data-clipboard-text]').forEach(b=>{
  if(!vis(b)) return;
  const t=b.getAttribute('data-clipboard-text'); if(t) out.push(t);
});

// devsite-code shadow + light DOM
root.querySelectorAll('devsite-code,devsite-snippet').forEach(host=>{
  if(!vis(host)) return;
  const sr=host.shadowRoot;
  if(sr){ sr.querySelectorAll('pre,code').forEach(n=>{ const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t); }); }
  host.querySelectorAll('pre,code').forEach(n=>{ const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t); });
});

// 일반 pre
root.querySelectorAll('pre').forEach(n=>{
  if(n.closest('table,devsite-code,devsite-snippet')) return;
  if(!vis(n)) return;
  const t=(n.innerText||n.textContent||'').trim(); if(t) out.push(t);
});
return out;
        """, root) or []

        return [c for c in codes if c.strip() and not _looks_like_tab_labels(c)]
    except Exception:
        return []


# ========= 순서 보존 페이지 텍스트 수집 =========
def collect_page_text_sequential(article) -> str:
    """원본 문서의 순서를 보존하면서 내용 수집"""
    parts = []

    try:
        # article 내의 모든 직접 자식 요소들을 순서대로 처리
        body = article.find_element(By.CSS_SELECTOR, ".devsite-article-body") if article else article
    except Exception:
        body = article

    try:
        all_elements = body.find_elements(By.XPATH, "./*")
    except Exception:
        return "수집 실패: 요소를 찾을 수 없습니다."

    current_section = []
    section_title = "본문"

    for element in all_elements:
        try:
            tag_name = (element.tag_name or "").lower()
            element_content = []

            # 헤딩 태그면 새 섹션 시작
            if tag_name in ("h2", "h3"):
                # 이전 섹션 저장
                if current_section:
                    parts.append(f"## {section_title}\n" + "\n\n".join(current_section))
                    current_section = []

                section_title = (element.text or "").strip() or "섹션"
                continue

            # 1. 표 처리
            if tag_name == "table" or element.find_elements(By.CSS_SELECTOR, "table"):
                table_md = _extract_single_table_md(element)
                if table_md:
                    element_content.append(f"### 표\n{table_md}")

            # 2. 탭 그룹 처리 (devsite-selector, devsite-tabs 등)
            elif (tag_name in ("devsite-selector", "devsite-tabs") or
                  element.find_elements(By.CSS_SELECTOR, '[role="tablist"], .devsite-tabs')):

                tab_content = _process_tab_group_sequential(element)
                if tab_content:
                    element_content.extend(tab_content)

            # 3. 일반 코드 블록
            elif (tag_name == "pre" or
                  element.find_elements(By.CSS_SELECTOR, "pre, devsite-code, devsite-snippet")):

                codes = _visible_codes_in(element)
                for i, code in enumerate(codes, 1):
                    lang_label = _guess_lang_label(code, "코드")
                    element_content.append(f"언어: {lang_label}\n{_norm_text(code)}")

            # 4. 일반 텍스트 (p, li, div 등)
            else:
                text_content = _extract_element_text(element)
                if text_content:
                    element_content.append(text_content)

            # 요소 내용이 있으면 현재 섹션에 추가
            if element_content:
                current_section.extend(element_content)

        except StaleElementReferenceException:
            continue
        except Exception as e:
            print(f"요소 처리 중 오류: {e}")
            continue

    # 마지막 섹션 저장
    if current_section:
        parts.append(f"## {section_title}\n" + "\n\n".join(current_section))

    final_content = "\n\n".join(parts).strip()

    # 내용이 너무 적으면 fallback
    if len(final_content) < 100:
        try:
            fallback = driver.execute_script("return (arguments[0].innerText||'').trim()", article)
            if len(fallback) > len(final_content):
                return "## Fallback\n" + _norm_text(fallback)
        except Exception:
            pass

    return final_content


def _extract_single_table_md(element):
    """단일 표를 마크다운으로 변환"""
    try:
        table_data = driver.execute_script("""
const el = arguments[0];
const table = el.tagName === 'TABLE' ? el : el.querySelector('table');
if (!table) return null;

const headers = [];
const ths = table.querySelectorAll('thead th, tr:first-child th');
ths.forEach(th => headers.push((th.innerText||th.textContent||'').trim()));

const rows = [];
const trs = table.querySelectorAll('tbody tr, tr');
trs.forEach(tr => {
  const cells = [];
  tr.querySelectorAll('td,th').forEach(td => {
    cells.push((td.innerText||td.textContent||'').trim());
  });
  if (cells.length) rows.push(cells);
});

return {headers, rows};
        """, element)

        if not table_data or not table_data.get('rows'):
            return ""

        headers = table_data.get('headers', [])
        rows = table_data.get('rows', [])

        # 마크다운 표 생성
        if not headers and rows:
            headers = [f"열{i + 1}" for i in range(len(rows[0]))]

        md_lines = []
        if headers:
            md_lines.append("| " + " | ".join(h.replace("|", "\\|") for h in headers) + " |")
            md_lines.append("| " + " | ".join("---" for _ in headers) + " |")

        for row in rows:
            clean_row = [str(cell).replace("|", "\\|") for cell in row]
            md_lines.append("| " + " | ".join(clean_row) + " |")

        return "\n".join(md_lines)

    except Exception:
        return ""


def _process_tab_group_sequential(element):
    """탭 그룹을 순서대로 처리"""
    tab_contents = []

    try:
        # 탭 리스트 찾기
        tablist = element
        if element.tag_name != "devsite-selector":
            tablist = element.find_element(By.CSS_SELECTOR, '[role="tablist"], .devsite-tabs')

        # 모든 탭 버튼 찾기
        tabs = []
        tabs.extend(tablist.find_elements(By.CSS_SELECTOR, '[role="tab"]'))
        tabs.extend(tablist.find_elements(By.CSS_SELECTOR, 'button, a, li>button, li>a, tab'))
        tabs = [t for t in tabs if t.is_displayed()]

        processed_labels = set()

        for i, tab in enumerate(tabs):
            try:
                label = _tab_label(tab, f"Tab{i + 1}")
                if label in processed_labels:
                    continue

                # 탭 클릭
                driver.execute_script("arguments[0].scrollIntoView({block:'center'});", tab)
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.1)

                # 패널 찾기
                panel = _find_panel_for_tab(tab, tablist)
                codes = _visible_codes_in(panel)

                if codes:
                    for j, code in enumerate(codes, 1):
                        tab_contents.append(f"언어: {label} · 셀#{j}\n{_norm_text(code)}")

                processed_labels.add(label)

            except Exception:
                continue

        # 더보기 버튼 처리 (간단 버전)
        try:
            more_btn = tablist.find_element(By.CSS_SELECTOR, "[aria-label*='더보기'], [aria-label*='More']")
            if more_btn and more_btn.is_displayed():
                # 더보기 처리 로직은 복잡하므로 기본적인 처리만
                pass
        except Exception:
            pass

    except Exception as e:
        print(f"탭 처리 중 오류: {e}")

    return tab_contents


def _extract_element_text(element):
    """요소에서 텍스트 추출 (표, 탭, 코드 제외)"""
    try:
        text = driver.execute_script("""
const el = arguments[0];
if (el.closest('table, [role="tablist"], [role="tabpanel"], devsite-code, pre, code')) return '';

const texts = [];
el.querySelectorAll('p, li, dt, dd, blockquote').forEach(textEl => {
  if (!textEl.closest('table, [role="tablist"], [role="tabpanel"], devsite-code, pre, code')) {
    const t = (textEl.innerText || textEl.textContent || '').trim();
    if (t) texts.push(t);
  }
});

if (texts.length === 0) {
  const directText = (el.innerText || el.textContent || '').trim();
  if (directText && directText.length > 10) return directText;
}

return texts.join('\\n\\n');
        """, element) or ""

        text = _norm_text(text)
        if text and not _looks_like_tab_labels(text):
            return text
    except Exception:
        pass
    return ""


# ========= 기타 필요한 함수들 =========
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

            return collect_page_text_sequential(article)

        except (TimeoutException, StaleElementReferenceException) as e:
            print(f"  재시도 사유: {type(e).__name__}", flush=True)
            if attempt >= retries: raise
            try:
                driver.refresh()
            except Exception:
                pass
            time.sleep(0.6)
        except WebDriverException:
            if attempt >= retries: raise
            print("  드라이버 재시작…", flush=True)
            try:
                driver.quit()
            except Exception:
                pass
            driver = setup_driver()
            wait = WebDriverWait(driver, 12)
            time.sleep(0.5)
        except Exception as e:
            print(f"  일반 오류: {e}", flush=True)
            if attempt >= retries: raise
            time.sleep(0.5)


# ========= 메인 크롤링 함수 =========
def crawl():
    q = deque([normalize_url(u) for u in START_URLS])
    visited, discovered = set(), set(q)
    pages_crawled = 0

    while q and pages_crawled < MAX_PAGES:
        url = q.popleft()
        if url in visited:
            continue

        print(f"\n({pages_crawled + 1}) 크롤링: {url}", flush=True)

        try:
            page_text = _load_and_collect_with_retry(url, retries=1)

            if not (page_text or "").strip():
                try:
                    art = driver.find_element(By.TAG_NAME, "article")
                except Exception:
                    art = driver.find_element(By.CSS_SELECTOR, "main, body")
                txt = driver.execute_script("return (arguments[0].innerText||'').trim()", art) or (art.text or "")
                if txt:
                    page_text = "## Fallback\n" + _norm_text(txt)

            filepath = os.path.join(OUTPUT_DIR, url_to_safe_filename(url))
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(f"Source URL: {url}\n\n{page_text}")
            print(f"저장 완료: {filepath}", flush=True)

            visited.add(url)
            pages_crawled += 1

            # 다음 링크들 수집
            next_links = []
            for norm in extract_all_page_links():
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


if __name__ == "__main__":
    try:
        crawl()
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        print("브라우저 종료", flush=True)