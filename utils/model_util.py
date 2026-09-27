
import os

from langchain.chat_models import init_chat_model
from dotenv import load_dotenv
load_dotenv()

ENDPOINT = os.getenv("ENDPOINT") if os.getenv("ENDPOINT") else "http://192.168.3.28:8088"
API_KEY = os.getenv("API_KEY") if os.getenv("API_KEY") else None
TEMPERATURE = float(os.getenv("TEMPERATURE")) if os.getenv("TEMPERATURE") else 0.5
TOP_P = float(os.getenv("TOP_P")) if os.getenv("TOP_P") else 0.5
MAX_TOKENS = int(os.getenv("MAX_TOKENS")) if os.getenv("MAX_TOKENS") else 65535

MAX_RETRY = int(os.getenv("MAX_RETRY")) if os.getenv("MAX_RETRY") else 3
MODEL_PROVIDER = os.getenv("MODEL_PROVIDER") if os.getenv("MODEL_PROVIDER") else "openai"
MODEL_NAME = os.getenv("MODEL_NAME") if os.getenv("MODEL_NAME") else "qwen3.8-9b"
TIMEOUT = float(os.getenv("TIMEOUT")) if os.getenv("TIMEOUT") else None

MODEL = init_chat_model(
    MODEL_NAME,
    model_provider=MODEL_PROVIDER,
    base_url=ENDPOINT,
    api_key=API_KEY,
    # custom_headers={"X-Custom-Header": "value"},
    timeout=TIMEOUT,
    max_retries=MAX_RETRY,
    temperature=TEMPERATURE,
    top_p=TOP_P,
    max_tokens=MAX_TOKENS,
)
