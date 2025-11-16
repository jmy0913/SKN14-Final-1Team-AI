from models.title_model import *

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

import json
import os, re, textwrap
from difflib import SequenceMatcher

from dotenv import load_dotenv

load_dotenv()

# 제목 요약을 위한 LangChain용 LLM (모델명은 상황에 맞게)
title_llm = ChatOpenAI(
    model="gpt-4o-mini",  # 또는 OPENAI_TITLE_MODEL
    temperature=0.2,
    max_tokens=30,
)

# 프롬프트 템플릿 정의
title_prompt = PromptTemplate.from_template(
    """
아래 대화 맥락과 기존 제목을 참고하여 **짧고 간결한 한국어 대화 제목**을 최종 확정하라.
- 글자 수: 12자 이상, 24자 이하
- 반드시 명사/주제어 위주 (불필요한 수식어 제거)
- 이모지, 따옴표, 마침표, 물음표, 느낌표, 특수문자 금지
- 접두사/접미사/콜론/괄호 금지
- 기존 제목(draft_title)이 이미 간결하고 적절하면 그대로 유지
- 문장을 그대로 복붙하지 말고, 맥락에서 핵심 주제만 뽑아 제목화
- 답변은 오직 제목 텍스트만 출력 (설명, 접두어, 여분 텍스트 금지)

기존 제목(draft_title): {draft_title}
최근 대화(context):
{transcript}
"""
)

# 체인 구성
title_chain = title_prompt | title_llm | StrOutputParser()

# 제목 요약 #
# PRODUCTS = r"(Google Sheets|Sheets|Gmail|Drive|Calendar|Maps|Docs|Slides)"
# KEYWORDS  = r"(batchUpdate|insert|list|update|auth|quota|range|scope|error|permission)"

OPENAI_TITLE_MODEL = os.getenv("OPENAI_TITLE_MODEL", "gpt-4o-mini")


def rule_title_fallback(text: str) -> str:
    """
    LLM 미사용/실패 시 간단 요약 폴백
    """
    s = re.split(r"[.\n!?]", text.strip())[0] or text.strip()
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"[^\w\sㄱ-ㅎ가-힣A-Za-z0-9]", "", s)
    return (s[:24]).strip() or "새 대화"


def sanitize_title(s: str) -> str:
    """
    이모지/제어문자 제거 + 길이 제한
    """
    s = re.sub(r"[^\w\s\-\:\.\,\[\]\(\)ㄱ-ㅎ가-힣A-Za-z0-9/]", "", s)
    return s.strip()[:60] or "General"


def norm(s: str) -> str:
    if not s:
        return ""
    s = s.lower()
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"[^\wㄱ-ㅎ가-힣]", "", s)


def tokens(s: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9가-힣]+", s.lower())


def is_echo_like(
        title: str, source: str, *, hard_ratio: float = 0.9, token_ratio: float = 0.8
) -> bool:  # NEW
    """질문 원문을 거의 그대로 베낀 제목인지 판정"""
    t, s = norm(title), norm(source)
    if not t or not s:
        return False
    if t in s:  # 부분 복붙
        return True
    if SequenceMatcher(None, t, s).ratio() >= hard_ratio:  # 문자 유사도
        return True
    # 토큰 자카드
    A, B = set(tokens(title)), set(tokens(source))
    if A and B and len(A & B) / len(A | B) >= token_ratio:
        return True
    return False


async def initial_title_with_llm(request: InitialTitleRequest):
    """
    첫 user 질문만으로 LLM이 임시 제목 생성
    OPENAI_API_KEY 없거나 실패하면 규칙 기반으로 폴백
    """

    first_question = request.first_content

    api_key = os.getenv("OPENAI_API_KEY")

    if not api_key:
        return rule_title_fallback(first_question)

    prompt = textwrap.dedent(
        f"""
      다음 문장을 바탕으로 한국어로 **짧고 간결한 대화 제목**을 하나만 만들어라.
      절대 원문 문장을 그대로 복사하지 말고, 핵심 주제를 명사 중심으로 추출하라.

      규칙:                      
      - 글자 수: 12자 이상, 24자 이하
      - 반드시 명사/주제어 위주 (불필요한 수식어 제거)
      - 이모지, 따옴표, 마침표, 물음표, 느낌표, 특수문자 금지
      - 접두사·접미사, 괄호, 콜론 금지
      - 문장 그대로 복사 후 붙여넣기 하지 말고, 핵심 키워드만 뽑아서 제목화
      - 질문 원문을 절대 그대로 베끼지 말 것 (핵심 개념만 압축)
      - 답변은 오직 제목 텍스트만 출력 (불필요한 설명·접두어 금지)

      예시:
      - 입력: "구글 시트에서 권한 오류가 자꾸 나요"
        출력: 구글 시트 권한 오류
      - 입력: "Drive API에서 파일 리스트 가져오는 법"
        출력: Drive API 파일 목록 조회
      - 입력: "gpt 호출쿼터 초과되면 어떻게 해야함?"
        출력: OpenAI 쿼터 초과 대응

      사용자 첫 질문: {first_question}
    """
    ).strip()

    try:
        print("[title] initial via LLM")
        import requests

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        body = {
            "model": OPENAI_TITLE_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": "Return only the title text. No punctuation at the end.",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
            "max_tokens": 30,
        }
        r = requests.post(url, headers=headers, json=body, timeout=12)
        if not r.ok:
            print("[title] status:", r.status_code, "body:", r.text[:300])
            r.raise_for_status()
        raw = r.json()["choices"][0]["message"]["content"]
        title = sanitize_title(raw)

        if is_echo_like(title, first_question) or len(tokens(title)) < 2:
            return rule_title_fallback(first_question)
        return title
    except Exception as e:
        print("[title] fallback used:", e)
        return rule_title_fallback(first_question)



async def refine_title_with_llm(request: RefineTitleRequest):
    """
    LangChain 기반 제목 리파인
    """

    draft_title = request.draft_title
    transcript = request.transcript


    if not os.getenv("OPENAI_API_KEY"):
        return draft_title

    try:
        result = title_chain.invoke(
            {"draft_title": draft_title, "transcript": transcript}
        ).strip()

        # 후처리 (기존 로직 유지)

        title = sanitize_title(result)

        user_lines = [
            ln[3:].strip() for ln in transcript.splitlines() if ln.startswith("Q:")
        ]

        for q in user_lines:
            if is_echo_like(title, q):
                return draft_title

        if is_echo_like(title, draft_title, hard_ratio=0.95, token_ratio=0.9):
            return draft_title

        if len(tokens(title)) < 2:
            return draft_title

        return title

    except Exception as e:
        print(f"[title refinement error] {str(e)}")
        return draft_title

