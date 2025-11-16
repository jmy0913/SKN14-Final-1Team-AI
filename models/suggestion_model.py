from typing import Dict, List, Literal, Optional
from pydantic import BaseModel



class SuggestionRequest(BaseModel):
    user_q: str
    answer: str
    k: int = 5

