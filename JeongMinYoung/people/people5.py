import os
import time
import re
import urllib.parse
from collections import deque
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, StaleElementReferenceException

# -------------------------------
# 설정
# -------------------------------
START_URLS = [
    "https://developers.google.com/people/api/rest?hl=ko",
    "https://developers.google.com/people/v1/profiles?hl=ko",
]
SAVE_DIR = "people_docs_crawled"
WAIT_TIME = 3

# 언어 라벨 → 실제 DOM class/속성 키 매핑
LANGUAGE_ALIASES = {
    "자바": ["java"],
    "파이썬": ["python", "py"],
    "프로토콜": ["http", "rest", "protocol"],
    "java": ["java"],
    "python": ["python", "py"],
    "php": ["php"],
    ".net": ["csharp", "dotnet", "cs", "c#"],
    "c#": ["csharp", "dotnet", "cs"],
    "kotlin": ["kotlin"],
    "objective-c": ["objective-c", "objc"],
    "swift": ["swift"],
    "go": ["go", "golang"],
}
def _lang_candidates(label: str):
    key = (label or "").strip().lower()
    key = key.replace(" ", "")
    return LANGUAGE_ALIASES.get(key, [key])

# -------------------------------
# WebDriver 준비
# -------------------------------
chrome_options = Options()
chrome_options.add_argument("--headless=new")
chrome_options.add_argument("--no-sandbox")
chrome_options.add_argument("--disable-dev-shm-usage")

driver = webdriver.Chrome(options=chrome_options)
wait = WebDriverWait(driver, WAIT_TIME)

if not os.path.exists(SAVE_DIR):
    os.makedirs(SAVE_DIR)

# -------------------------------
# 유틸 함수
# -------------------------------
def sanitize_filename(url):
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", url)

def _retry_stale(fn, retries=3, delay=0.3):
    for i in range(retries):
        try:
            return fn()
        except StaleElementReferenceException:
            time.sleep(delay)
    raise

# -------------------------------
# 코드 추출 (Shadow DOM 포함)
# -------------------------------
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

def _visible_code_in(scope):
    try:
        code_blocks = scope.find_elements(By.CSS_SELECTOR, "pre, code")
        texts = [cb.text for cb in code_blocks if cb.is_displayed() and cb.text.strip()]
        return "\n\n".join(texts).strip() if texts else ""
    except Exception:
        return ""

def _visible_code_by_language(label):
    candidates = _lang_candidates(label)
    for cand in candidates:
        sel_list = [
            f'[data-language="{cand}"] pre',
            f'[data-language="{cand}"] code',
            f'[data-code-lang="{cand}"] pre',
            f'[data-code-lang="{cand}"] code',
            f'[data-lang="{cand}"] pre',
            f'[data-lang="{cand}"] code',
        ]
        for sel in sel_list:
            try:
                nodes = driver.find_elements(By.CSS_SELECTOR, sel)
                texts = [n.text for n in nodes if n.is_displayed() and n.text.strip()]
                if texts:
                    return "\n\n".join(texts).strip()
            except Exception:
                continue
    try:
        hosts = driver.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
        if hosts:
            txt = _shadow_texts_from_hosts(hosts, lang_like_list=candidates)
            if txt:
                return txt
    except Exception:
        pass
    return ""

def _visible_code_after(tab_el, section_scope, before_text, wait, lang_label):
    try:
        driver.execute_script("arguments[0].scrollIntoView({block:'center'});", section_scope)
        time.sleep(0.1)
    except Exception:
        pass
    panel_id = tab_el.get_attribute("aria-controls")
    if panel_id:
        try:
            panel = _retry_stale(lambda: driver.find_element(By.CSS_SELECTOR, f'#{panel_id}'))
            code_blocks = _retry_stale(lambda: panel.find_elements(By.CSS_SELECTOR, "pre, code"))
            texts = [cb.text for cb in code_blocks if cb.is_displayed() and cb.text.strip()]
            try:
                hosts = panel.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet")
                if hosts:
                    txt2 = _shadow_texts_from_hosts(hosts, lang_like_list=_lang_candidates(lang_label))
                    if txt2:
                        texts.append(txt2)
            except Exception:
                pass
            if texts:
                return "\n\n".join(texts).strip()
            if panel.text.strip():
                return panel.text.strip()
        except Exception:
            pass
    def changed_code(_):
        txt = _visible_code_in(section_scope)
        return txt if (txt and txt != before_text) else False
    try:
        changed = wait.until(changed_code)
        if changed:
            return changed
    except TimeoutException:
        pass
    txt = _visible_code_by_language(lang_label)
    if txt:
        return txt
    curr = _visible_code_in(section_scope)
    if curr:
        return curr
    return before_text or ""

# -------------------------------
# 메인 크롤러
# -------------------------------
visited = set()
q = deque(START_URLS)

while q:
    url = q.popleft()
    if url in visited:
        continue
    visited.add(url)
    print(f"\n크롤링: {url}")

    try:
        driver.get(url)
    except Exception as e:
        print(f"[로드 실패] {url} : {e}")
        continue

    time.sleep(1)
    page_texts = []

    # 탭 코드 스니펫 수집
    sections = driver.find_elements(By.CSS_SELECTOR, "devsite-code, devsite-snippet, devsite-tabs, .devsite-code-tab, .ds-tab")
    for sec in sections:
        tabs = []
        try:
            tabs = sec.find_elements(By.CSS_SELECTOR, "[role=tab]")
        except Exception:
            pass
        if not tabs:
            code = _visible_code_in(sec)
            if code:
                page_texts.append(code)
            continue
        for tab in tabs:
            lang_label = (tab.text or "").strip()
            if not lang_label:
                continue
            before = _visible_code_in(sec)
            try:
                driver.execute_script("arguments[0].click();", tab)
                time.sleep(0.2)
            except Exception:
                continue
            code = _visible_code_after(tab, sec, before, wait, lang_label)
            if code:
                entry = f"{lang_label} - {code}"
                page_texts.append(entry)
            else:
                print(f"[tabs] '{lang_label}' 코드 스니펫을 찾지 못함")

    # 본문 텍스트
    body_text = driver.find_element(By.TAG_NAME, "body").text.strip()
    if body_text:
        page_texts.append("[본문]\n" + body_text)

    # 저장
    fname = sanitize_filename(url) + ".txt"
    fpath = os.path.join(SAVE_DIR, fname)
    with open(fpath, "w", encoding="utf-8") as f:
        f.write("\n\n".join(page_texts))
    print(f"저장 완료: {fpath}")

    # 새 링크 수집
    links = driver.find_elements(By.TAG_NAME, "a")
    new_links = []
    for link in links:
        href = link.get_attribute("href")
        if not href:
            continue
        if href.startswith("https://developers.google.com/people/") and href not in visited:
            new_links.append(href)
            q.append(href)
    if new_links:
        print(f"  ↳ 새 링크 {len(new_links)}개 추가 (대기열 {len(q)})")

driver.quit()
