"""Settings for the agent service, read from the environment (and an optional .env file at the repo root)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENT_NAMES = (
    "orchestrator",
    "recruitment",
    "comms",
    "assistant",
    "leave",
    "onboarding",
    "payroll",
    "performance",
    "conflict",
    "operator",
)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing environment wins. No secrets are ever logged."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ.setdefault(key.strip(), value)


@dataclass(frozen=True)
class Settings:
    rhflow_url: str
    ollama_url: str
    model: str
    temperature: float
    num_ctx: int
    dataset_dir: Path
    var_dir: Path
    max_tool_calls: int
    max_steps: int
    max_tokens: int
    max_proposals: int
    propose_to_rhflow: bool
    mail_source: str
    mailpit_url: str
    hr_inbox_address: str
    input_layer: str
    ingest_url: str
    ingest_envelope_key: str
    ingest_signing_key: str
    webhook_secret: str
    webhook_window: int
    server_host: str
    server_port: int

    def credentials(self, agent: str) -> tuple[str, str] | None:
        """Per-agent read-only API key/secret (RHFLOW_<AGENT>_API_KEY / _API_SECRET)."""
        key = os.environ.get(f"RHFLOW_{agent.upper()}_API_KEY")
        secret = os.environ.get(f"RHFLOW_{agent.upper()}_API_SECRET")
        return (key, secret) if key and secret else None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    _load_dotenv(REPO_ROOT / ".env")
    env = os.environ.get
    return Settings(
        rhflow_url=env("RHFLOW_URL", "http://localhost:8001").rstrip("/"),
        ollama_url=env("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        model=env("LLM_MODEL", "qwen3:8b"),
        temperature=float(env("LLM_TEMPERATURE", "0.1")),
        num_ctx=int(env("LLM_NUM_CTX", "16384")),
        dataset_dir=Path(env("DATASET_DIR", str(Path.home() / "frappe_docker/Data_set/dataset"))),
        var_dir=Path(env("AGENTS_VAR_DIR", str(REPO_ROOT / "var"))),
        max_tool_calls=int(env("AGENT_MAX_TOOL_CALLS", "6")),
        max_steps=int(env("AGENT_MAX_STEPS", "8")),
        max_tokens=int(env("AGENT_MAX_TOKENS", "60000")),
        max_proposals=int(env("AGENT_MAX_PROPOSALS", "10")),
        propose_to_rhflow=env("RHFLOW_PROPOSE_ENABLED", "0") == "1",
        mail_source=env("MAIL_SOURCE", "folder"),
        mailpit_url=env("MAILPIT_URL", "http://localhost:8025").rstrip("/"),
        hr_inbox_address=env("HR_INBOX_ADDRESS", "hr@rhflow.local"),
        # "service": CVs and emails come from the input security layer (signed, masked envelopes);
        # "local": the agents' own ingest pipeline (offline fallback, used by the tests).
        input_layer=env("INPUT_LAYER", "local"),
        ingest_url=env("INGEST_URL", "http://localhost:8090").rstrip("/"),
        ingest_envelope_key=env("INGEST_ENVELOPE_API_KEY", ""),
        ingest_signing_key=env("INGEST_SIGNING_KEY", ""),
        # Signed calls from RH Flow (hr_ai/webhooks.py) to agents/integration/server.py.
        webhook_secret=env("WEBHOOK_HMAC_SECRET", ""),
        webhook_window=int(env("WEBHOOK_REPLAY_WINDOW_SECONDS", "300")),
        server_host=env("AGENTS_HOST", "127.0.0.1"),
        server_port=int(env("AGENTS_PORT", "8200")),
    )
