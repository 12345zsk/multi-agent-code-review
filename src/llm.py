from langchain_deepseek import ChatDeepSeek
from dotenv import load_dotenv
import os

from src.config import FAST_MODEL


def get_fast_llm():
    load_dotenv()
    key = os.getenv("DEEPSEEK_API_KEY", "")
    if not key or key in {"YOUR_API_KEY_HERE", "YOUR_TOKEN_HERE"}:
        raise ValueError("Set DEEPSEEK_API_KEY before running a model review")
    return ChatDeepSeek(
        model=os.getenv("DEEPSEEK_MODEL", FAST_MODEL),
        temperature=0,
        timeout=60,
        max_retries=2,
    )
