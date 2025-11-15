from fastapi import APIRouter
from models.chat_model import ChatRequest, ChatRequest2
from services.langchain_service import chat_service
from services.langgraph_service import run_langraph

router = APIRouter()


@router.post("/chat")
async def chat(chat_request: ChatRequest):
    response, title, tool_calls, tool_responses = await chat_service.get_chat_response(chat_request)
    return {"response": response, "title": title, "tool_calls": tool_calls, "tool_responses": tool_responses}


@router.post("/chat2")
async def chat2(chat_request: ChatRequest2):
    response = await run_langraph(chat_request)
    return {"response": response}



