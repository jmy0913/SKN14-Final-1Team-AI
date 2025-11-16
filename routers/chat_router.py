from fastapi import APIRouter
from models.chat_model import ChatRequest, ChatRequest2
from models.title_model import InitialTitleRequest, RefineTitleRequest
from models.suggestion_model import SuggestionRequest
from models.query_model import QueryRequest
from services.langchain_service import chat_service
from services.langgraph_service import run_langraph
from services.title_llm_service import initial_title_with_llm, refine_title_with_llm
from services.suggest_llm_service import generate_suggestions
from services.query_service import search_dense

router = APIRouter()


@router.post("/chat")
async def chat(chat_request: ChatRequest):
    response, title, tool_calls, tool_responses = await chat_service.get_chat_response(chat_request)
    return {"response": response, "title": title, "tool_calls": tool_calls, "tool_responses": tool_responses}


@router.post("/chat2")
async def chat2(chat_request: ChatRequest2):
    response = await run_langraph(chat_request)
    return {"response": response}


@router.post("/title")
async def title(title_request: InitialTitleRequest):
    title = await initial_title_with_llm(title_request)
    return {"title": title}



@router.post("/title2")
async def title2(title_request: RefineTitleRequest):
    title = await refine_title_with_llm(title_request)
    return {"title": title}



@router.post("/suggest")
async def suggest(suggest_request: SuggestionRequest):
    suggestions = await generate_suggestions(suggest_request)
    return {"suggestions": suggestions}


@router.post("/query")
async def query(query: QueryRequest):
    rows = await search_dense(query)
    return {"rows": rows}