from typing import Dict, List, Literal, Optional
from pydantic import BaseModel



class InitialTitleRequest(BaseModel):
    first_content: str


class RefineTitleRequest(BaseModel):
    draft_title: str
    transcript: str

