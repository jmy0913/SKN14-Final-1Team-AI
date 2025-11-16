from typing import Dict, List, Literal, Optional
from pydantic import BaseModel


class ChatRequest(BaseModel):
    history: List[Dict]
    permission: Literal["cto", "backend", "frontend", "data_ai", "none"] = "none"
    tone: Literal["formal", "informal"] = "formal"


class ChatRequest2(BaseModel):
    user_input: str
    config_id: str
    image: Optional[str] = None
    chat_history: List[Dict] = []
