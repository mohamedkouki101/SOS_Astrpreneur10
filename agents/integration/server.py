"""Agent service HTTP receiver for signed calls from HR Flow (hr_ai/webhooks.py). FastAPI.

    POST /chat    HR Chat message (synchronous: HR Flow's background job waits for the reply)
    POST /events  doc events, e.g. inbound_email.received (queued, 202 returned at once)
    GET  /health

Every POST is verified (HMAC, replay window, event id) before anything else, and each event id runs
once: a replay or a retry returns the stored result (or "duplicate") without running agents again.
The agents behind it keep their own read-only credentials; this server holds no write access.

Run (WSL, repo root):  $HOME/.venvs/rhflow-agents/bin/python -m agents.integration.server
Binds to 127.0.0.1:8200 by default (AGENTS_HOST / AGENTS_PORT); containers reach it through
host.docker.internal.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Callable

import anyio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from agents.config import get_settings
from agents.integration.signing import EventStore, SignatureError, verify
from agents.orchestrator import audit

MAX_BODY = 64 * 1024
log = logging.getLogger("rhflow.agents.server")


def create_app(*, secret: str, window: int, store: EventStore, chat_handler: Callable[[dict], dict],
               event_handler: Callable[[dict], dict], run_worker: bool = True, retries: int = 3,
               retry_delay: float = 30.0) -> FastAPI:
    app = FastAPI(title="HR Flow agent service", docs_url=None, redoc_url=None, openapi_url=None)
    llm_lock = threading.Lock()          # one model run at a time (single local GPU / Ollama)
    jobs: queue.Queue[dict] = queue.Queue()

    async def authentic(request: Request) -> dict:
        body = await request.body()
        if len(body) > MAX_BODY:
            raise HTTPException(413, "body too large")
        try:
            return verify(dict(request.headers), body, secret, window)
        except SignatureError as exc:
            audit.alert("webhook_rejected", path=request.url.path, reason=str(exc))
            raise HTTPException(401, "unauthorized") from None

    def run(event: dict, handler: Callable[[dict], dict]) -> dict:
        try:
            with llm_lock:
                result = handler(event)
        except Exception as exc:
            store.finish(event["event_id"], "failed", {"error": type(exc).__name__})
            audit.log("webhook_failed", event_id=event["event_id"], error=f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        store.finish(event["event_id"], "done", result)
        audit.log("webhook_done", event_id=event["event_id"], proposals=result.get("proposals"))
        return result

    def worker():
        while True:
            event = jobs.get()
            attempt = event.pop("_attempt", 1)
            try:
                run(event, event_handler)
            except Exception:
                log.exception("event %s failed (attempt %d)", event.get("event_id"), attempt)
                if attempt < retries:   # e.g. the model or HR Flow was briefly down
                    store.claim(event["event_id"], str(event.get("type", ""))[:60])
                    threading.Timer(retry_delay * attempt, jobs.put, args=({**event, "_attempt": attempt + 1},)).start()
            finally:
                jobs.task_done()

    if run_worker:
        threading.Thread(target=worker, name="rhflow-events", daemon=True).start()
    app.state.jobs = jobs
    app.state.worker = worker

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.post("/chat")
    async def chat(request: Request):
        event = await authentic(request)
        if event.get("type") != "chat.message":
            raise HTTPException(400, "unexpected event type")
        fresh, row = store.claim(event["event_id"], "chat.message")
        if not fresh:
            if row["status"] == "done":
                return {**row["result"], "duplicate": True}
            raise HTTPException(409, "this message is already being processed")
        audit.log("webhook_accepted", event_id=event["event_id"], type="chat.message")
        try:
            return await anyio.to_thread.run_sync(run, event, chat_handler)
        except Exception:
            raise HTTPException(500, "the assistant could not answer") from None

    @app.post("/events", status_code=202)
    async def events(request: Request):
        event = await authentic(request)
        fresh, row = store.claim(event["event_id"], str(event.get("type", ""))[:60])
        if not fresh:
            return JSONResponse({"status": "duplicate", "previous": row["status"]}, status_code=200)
        audit.log("webhook_accepted", event_id=event["event_id"], type=event.get("type"))
        jobs.put(event)
        return {"status": "queued"}

    return app


def build_app() -> FastAPI:
    from agents.integration.chat import handle_chat
    from agents.integration.events import handle_event

    s = get_settings()
    if not s.webhook_secret:
        raise SystemExit("WEBHOOK_HMAC_SECRET is not set (.env)")
    return create_app(secret=s.webhook_secret, window=s.webhook_window, store=EventStore(s.var_dir / "events.sqlite"),
                      chat_handler=handle_chat, event_handler=handle_event)


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    s = get_settings()
    uvicorn.run(build_app(), host=s.server_host, port=s.server_port, log_level="info")


if __name__ == "__main__":
    main()
