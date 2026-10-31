"""Wire everything together from config files + environment.

Run the service:
    cd guardrails
    ./venv/Scripts/python -m uvicorn guardrails.settings:create_app_from_env --factory --port 8100
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from .agent_layer import AgentLayerAdapter, FrappeDirectory
from .api import ApiConfig, Services, create_app
from .approval import ApprovalService, ApprovalSigner
from .auth import FrappeAuthenticator
from .fairness import FairnessChecker
from .frappe_client import FrappeClient
from .gateway import ExecutionGateway
from .monitor import ConsoleNotifier, Monitor
from .policy import PolicyEngine
from .storage import AuditLog, Database, SQLiteActionStore

BASE_DIR = Path(__file__).resolve().parents[1]


def load_env_file(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines). Real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _json(cfg_dir: Path, name: str) -> dict:
    return json.loads((cfg_dir / name).read_text(encoding="utf-8"))


def build_services(base_dir: Path = BASE_DIR) -> Services:
    cfg = base_dir / "config"
    db_path = Path(os.environ.get("GUARDRAILS_DB_PATH", "data/guardrails.db"))
    if not db_path.is_absolute():
        db_path = base_dir / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)

    db = Database(db_path)
    audit = AuditLog(db)
    policy = PolicyEngine.from_file(cfg / "policy.json")
    fairness = FairnessChecker.from_file(cfg / "fairness.json")
    approvals = ApprovalService.from_file(
        cfg / "approval.json", policy=policy, fairness=fairness,
        signer=ApprovalSigner.from_env(), store=SQLiteActionStore(db), audit=audit,
    )
    frappe = FrappeClient.from_env()
    gateway = ExecutionGateway(approvals, frappe)
    monitor = Monitor.from_file(cfg / "monitor.json", audit, notifiers=[ConsoleNotifier()],
                                fairness=fairness)
    criteria = {k: tuple(v) for k, v in _json(cfg, "job_criteria.json").items() if not k.startswith("_")}
    return Services(
        approvals=approvals, gateway=gateway, monitor=monitor,
        authenticator=FrappeAuthenticator(os.environ.get("FRAPPE_URL", "")),
        config=ApiConfig.parse(_json(cfg, "api.json")),
        job_criteria=criteria,
        agent_layer=AgentLayerAdapter(_json(cfg, "agent_layer.json"), FrappeDirectory(frappe)),
    )


def create_app_from_env():
    load_env_file(BASE_DIR / ".env")
    enable_docs = os.environ.get("GUARDRAILS_DISABLE_DOCS", "0") != "1"
    return create_app(build_services(), enable_docs=enable_docs)
