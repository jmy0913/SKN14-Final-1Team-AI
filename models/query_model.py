from typing import Dict, List, Literal, Optional
from pydantic import BaseModel



class QueryRequest(BaseModel):
    q: str
    k: int
