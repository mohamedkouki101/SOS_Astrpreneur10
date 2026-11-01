"""Guardrail: signed calls from RH Flow to the agent service (agents/integration/server.py).

Bad signature -> 401, stale timestamp (replay) -> 401, tampered body -> 401,
same event id twice -> the agents run ONCE (the stored result / "duplicate" is returned).
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from agents.integration.server import create_app
from agents.integration.signing import EventStore, sign

SECRET = "test-secret-not-real"


def _headers(body: bytes, event_id: str, ts: int | None = None, secret: str = SECRET) -> dict:
    ts = str(ts if ts is not None else int(time.time()))
    return {"Content-Type": "application/json", "X-RHFlow-Event-Id": event_id, "X-RHFlow-Timestamp": ts,
            "X-RHFlow-Signature": sign(secret, ts, event_id, body)}


@pytest.fixture
def service(tmp_path):
    calls = {"chat": 0, "event": 0}

    def chat_handler(event):
        calls["chat"] += 1
        return {"reply": "hello", "persona": "unknown", "proposals": [], "run_id": "r1"}

    def event_handler(event):
        calls["event"] += 1
        return {"answer": "ok", "proposals": []}

    app = create_app(secret=SECRET, window=300, store=EventStore(tmp_path / "events.sqlite"),
                     chat_handler=chat_handler, event_handler=event_handler, run_worker=False)
    return TestClient(app), calls, app


def _chat_body(event_id="chat:CHAT-2026-00001"):
    return json.dumps({"event_id": event_id, "type": "chat.message", "message": "CHAT-2026-00001",
                       "report_id": "sha256:" + "a" * 64}).encode()


def test_valid_signature_runs_once(service):
    client, calls, _ = service
    body = _chat_body()
    r = client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001"))
    assert r.status_code == 200 and r.json()["reply"] == "hello"
    assert calls["chat"] == 1


def test_bad_signature_rejected(service):
    client, calls, _ = service
    body = _chat_body()
    r = client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001", secret="wrong"))
    assert r.status_code == 401
    assert calls["chat"] == 0


def test_tampered_body_rejected(service):
    client, calls, _ = service
    body = _chat_body()
    headers = _headers(body, "chat:CHAT-2026-00001")
    tampered = body.replace(b"CHAT-2026-00001\"", b"CHAT-2026-00002\"", 1)
    assert client.post("/chat", content=tampered, headers=headers).status_code == 401
    assert calls["chat"] == 0


def test_stale_timestamp_replay_rejected(service):
    client, calls, _ = service
    body = _chat_body()
    old = int(time.time()) - 3600
    assert client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001", ts=old)).status_code == 401
    assert calls["chat"] == 0


def test_event_id_mismatch_rejected(service):
    client, calls, _ = service
    body = _chat_body("chat:CHAT-2026-00009")
    assert client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001")).status_code == 401


def test_duplicate_chat_processed_once(service):
    client, calls, _ = service
    body = _chat_body()
    first = client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001"))
    again = client.post("/chat", content=body, headers=_headers(body, "chat:CHAT-2026-00001"))
    assert first.status_code == again.status_code == 200
    assert again.json()["duplicate"] is True and again.json()["reply"] == "hello"
    assert calls["chat"] == 1


def test_duplicate_webhook_processed_once(service):
    client, calls, app = service
    body = json.dumps({"event_id": "inbound_email:IN-2026-00001:abc", "type": "inbound_email.received",
                       "name": "IN-2026-00001"}).encode()
    headers = _headers(body, "inbound_email:IN-2026-00001:abc")
    assert client.post("/events", content=body, headers=headers).status_code == 202
    assert client.post("/events", content=body, headers=headers).json()["status"] == "duplicate"
    assert app.state.jobs.qsize() == 1   # queued once, so the comms agent runs once


def test_failed_event_can_be_retried(tmp_path):
    store = EventStore(tmp_path / "e.sqlite")
    assert store.claim("x", "t") == (True, None)
    store.finish("x", "failed", {"error": "OllamaDown"})
    assert store.claim("x", "t")[0] is True        # a failure is not a reason to drop the event
    store.finish("x", "done", {"ok": 1})
    assert store.claim("x", "t") == (False, {"status": "done", "result": {"ok": 1}})


def test_failed_event_is_retried_then_runs_once(tmp_path):
    attempts = []

    def flaky(event):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("model down")
        return {"answer": "ok", "proposals": []}

    store = EventStore(tmp_path / "events.sqlite")
    app = create_app(secret=SECRET, window=300, store=store, chat_handler=lambda e: {}, event_handler=flaky,
                     run_worker=False, retry_delay=0.01)
    client = TestClient(app)
    body = json.dumps({"event_id": "inbound_email:IN-2026-00002:x", "type": "inbound_email.received",
                       "name": "IN-2026-00002"}).encode()
    assert client.post("/events", content=body, headers=_headers(body, "inbound_email:IN-2026-00002:x")).status_code == 202
    import threading
    threading.Thread(target=app.state.worker, daemon=True).start()
    for _ in range(200):
        if store.claim("inbound_email:IN-2026-00002:x", "t")[1] == {"status": "done", "result": {"answer": "ok", "proposals": []}}:
            break
        time.sleep(0.02)
    assert len(attempts) == 2
    # and a later duplicate delivery does not run it a third time
    assert client.post("/events", content=body, headers=_headers(body, "inbound_email:IN-2026-00002:x")).json()["status"] == "duplicate"
