"""HTTP server for the Fernhollow Health clinical-guardrails demo.

GET  /                              the chat page (index.html)
GET  /api/config                    company, preset attacks, examples, knowledge base titles
GET  /api/eval                      the latest recorded Chalk evaluation run (eval_results.json)
POST /api/conversations             start a conversation -> {"id", "greeting"}
POST /api/conversations/{id}/messages {"text"} -> NDJSON: one "stage" event per pipeline stage,
                                    then one "final" event with the reply and its audit

The models are reached through the Chalk model gateway with a router API key from a Chalk
secret. The page is public, so conversations, messages and message length are capped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, AsyncIterator, Iterator

import anthropic
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

import agent
import knowledge

PORT = int(os.environ.get("PORT", "8080"))
HERE = Path(__file__).resolve().parent
INDEX_HTML = HERE / "index.html"
EVAL_RESULTS = HERE / "eval_results.json"
API_SERVER = os.environ.get("CHALK_API_SERVER", "https://api.chalk.ai").rstrip("/")
ROUTER_BASE_URL = os.environ.get("ROUTER_BASE_URL", f"{API_SERVER}/v1/router")
FAST_MODEL = os.environ.get("FAST_MODEL", "anthropic/claude-haiku-5-5")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "anthropic/claude-sonnet-5-5")

MAX_TEXT_CHARS = 500
MAX_TURNS = 16
CONVERSATION_TTL_S = 30 * 60
MAX_ACTIVE_CONVERSATIONS = 300
CONVERSATIONS_PER_HOUR = 15
MESSAGES_PER_HOUR = 80

log = logging.getLogger("clinical-guardrails")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def model_config() -> agent.ModelConfig:
    key = os.environ["ROUTER_API_KEY"]
    # A router API key ("ck-...") goes in x-api-key; a Chalk access token is a bearer token.
    auth = {"api_key": key} if key.startswith("ck-") else {"auth_token": key, "api_key": None}
    client = anthropic.Anthropic(
        base_url=ROUTER_BASE_URL,
        default_headers={"X-Chalk-Env-Id": os.environ["CHALK_ENVIRONMENT"]},
        timeout=60.0,
        max_retries=2,
        **auth,
    )
    return agent.ModelConfig(client, FAST_MODEL, JUDGE_MODEL)


class Limiter:
    """Sliding one-hour window of events per key."""

    def __init__(self, per_hour: int) -> None:
        super().__init__()
        self.per_hour = per_hour
        self.events: dict[str, deque[float]] = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.time()
        with self.lock:
            q = self.events[key]
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= self.per_hour:
                return False
            q.append(now)
            return True


conversations: dict[str, agent.Conversation] = {}
turns: dict[str, int] = defaultdict(int)
locks: dict[str, threading.Lock] = {}
conversation_limiter = Limiter(CONVERSATIONS_PER_HOUR)
message_limiter = Limiter(MESSAGES_PER_HOUR)
cfg = model_config()
app = FastAPI()


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def reap_expired() -> None:
    now = time.time()
    for cid in [cid for cid, c in conversations.items() if now - c.started > CONVERSATION_TTL_S]:
        conversations.pop(cid, None)
        turns.pop(cid, None)
        locks.pop(cid, None)


def stream(events: Iterator[dict[str, Any]], lock: threading.Lock) -> StreamingResponse:
    """Runs a blocking event generator on a thread and streams each event as one JSON line.

    The per-conversation lock keeps two requests from interleaving turns on one conversation.
    """
    queue: asyncio.Queue[str | None] = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def put(item: str | None) -> None:
        asyncio.run_coroutine_threadsafe(queue.put(item), loop).result()

    def run() -> None:
        try:
            with lock:
                for event in events:
                    put(json.dumps(event) + "\n")
        except Exception as exc:  # noqa: BLE001 - reported to the page instead of a dropped stream
            log.exception("turn failed")
            put(json.dumps({"type": "error", "message": f"The assistant hit an error: {type(exc).__name__}"}) + "\n")
        finally:
            put(None)

    threading.Thread(target=run, daemon=True).start()

    async def body() -> AsyncIterator[str]:
        while (line := await queue.get()) is not None:
            yield line

    return StreamingResponse(body(), media_type="application/x-ndjson")


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML.read_text())


@app.get("/api/config")
async def config() -> JSONResponse:
    return JSONResponse(
        {
            "company": knowledge.COMPANY,
            "assistant": knowledge.ASSISTANT,
            "nurse_line": knowledge.NURSE_LINE,
            "attacks": knowledge.ATTACKS,
            "examples": knowledge.LOGISTICS_EXAMPLES,
            "documents": [{k: d[k] for k in ("id", "kind", "title")} for d in knowledge.DOCUMENTS],
            "limits": {"max_chars": MAX_TEXT_CHARS, "max_turns": MAX_TURNS},
            "models": {"fast": FAST_MODEL, "judge": JUDGE_MODEL},
        }
    )


@app.get("/api/eval")
async def eval_results() -> JSONResponse:
    if not EVAL_RESULTS.exists():
        raise HTTPException(404, "No evaluation has been recorded yet.")
    return JSONResponse(json.loads(EVAL_RESULTS.read_text()))


@app.post("/api/conversations")
async def start(request: Request) -> JSONResponse:
    reap_expired()
    if len(conversations) >= MAX_ACTIVE_CONVERSATIONS:
        raise HTTPException(503, "The demo is busy. Try again in a few minutes.")
    if not conversation_limiter.allow(client_ip(request)):
        raise HTTPException(429, f"That is {CONVERSATIONS_PER_HOUR} conversations this hour. Try again later.")
    conv = agent.Conversation(id=uuid.uuid4().hex)
    conversations[conv.id] = conv
    locks[conv.id] = threading.Lock()
    greeting = (
        f"Hi Alex, I'm {knowledge.ASSISTANT}, {knowledge.COMPANY}'s assistant. I can help with your "
        "plan, appointments, refills and finding care. I can't give medical advice."
    )
    return JSONResponse({"id": conv.id, "greeting": greeting})


class MessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)


@app.post("/api/conversations/{conversation_id}/messages")
async def message(conversation_id: str, req: MessageRequest, request: Request) -> StreamingResponse:
    conv = conversations.get(conversation_id)
    if conv is None:
        raise HTTPException(404, "This conversation has expired. Start a new one.")
    if turns[conversation_id] >= MAX_TURNS:
        raise HTTPException(429, f"This conversation has reached {MAX_TURNS} messages. Start a new one.")
    if not message_limiter.allow(client_ip(request)):
        raise HTTPException(429, f"That is {MESSAGES_PER_HOUR} messages this hour. Try again later.")
    turns[conversation_id] += 1
    text = req.text.strip()
    # Message text is never logged: visitors may type real health details into a public demo.
    log.info("turn %s #%d (%d chars)", conversation_id[:8], turns[conversation_id], len(text))
    return stream(agent.run_turn(conv, text, cfg), locks[conversation_id])


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
