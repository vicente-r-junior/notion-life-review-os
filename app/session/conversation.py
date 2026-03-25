import json
from app.session.redis_store import redis_client

HISTORY_TTL = 86400  # 24h


def get_history(phone: str) -> list:
    raw = redis_client.get(f"history:{phone}")
    return json.loads(raw) if raw else []


def append_history(phone: str, role: str, content: str):
    history = get_history(phone)
    history.append({"role": role, "content": content})
    history = history[-20:]  # keep last 20 messages
    redis_client.setex(f"history:{phone}", HISTORY_TTL, json.dumps(history))


def clear_history(phone: str):
    redis_client.delete(f"history:{phone}")
