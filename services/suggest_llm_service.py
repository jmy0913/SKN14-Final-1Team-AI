from models.suggestion_model import SuggestionRequest

from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import PromptTemplate
from langchain_openai import ChatOpenAI

import json
import os, re, textwrap
from difflib import SequenceMatcher

from dotenv import load_dotenv

load_dotenv()


# 연관 질문 추천 모델 지정 (환경변수 fallback)
suggest_model = os.getenv("OPENAI_SUGGEST_MODEL", "gpt-4o-mini")

# LangChain LLM 객체
suggest_llm = ChatOpenAI(
    model=suggest_model,
    temperature=0.4,
    max_tokens=150,
)

# 프롬프트 템플릿
suggest_prompt = PromptTemplate.from_template(
    """
너는 대화형 검색 보조도구야. 아래 '질문'과 '답변'을 보고
서로 다른 관점의 **후속 질문**을 최대 {k}개 만들어.

형식/규칙:
- 한국어, 12~30자, 간결한 명사/구문 중심
- 중복/의미 반복 금지, 너무 지엽적·랜덤 금지
- 다양한 관점(개념 설명, 단계, 코드, 오류 해결, 모범사례 등) 섞기
- 반드시 **JSON 배열**(예: ["...","..."])만 출력

[질문]
{user_q}

[답변]
{answer}
"""
)

# 체인 구성
suggest_chain = suggest_prompt | suggest_llm | StrOutputParser()


# 추천 질문 생성
async def generate_suggestions(request: SuggestionRequest):
    """
    LangChain 기반 후속 질문 생성 함수
    """

    user_q = request.user_q
    answer = request.answer
    k = request.k

    try:
        # LangChain chain 실행
        raw_output = suggest_chain.invoke(
            {
                "user_q": user_q,
                "answer": answer,
                "k": k,
            }
        ).strip()

        # JSON 파싱
        suggestions = json.loads(raw_output)

        # 후처리 (중복/길이/타입 체크)
        seen, out = set(), []
        for s in suggestions:
            if not isinstance(s, str):
                continue
            s = re.sub(r"\s+", " ", s).strip()
            if not (6 <= len(s) <= 40):
                continue
            if s in seen:
                continue
            seen.add(s)
            out.append(s)
            if len(out) >= k:
                break
        return out

    except Exception as e:
        print(f"[suggestion error] {e}")
        return []
