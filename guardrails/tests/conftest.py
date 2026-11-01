import pytest

from guardrails.approval import InMemoryActionStore
from guardrails.storage import AuditLog, Database, SQLiteActionStore


@pytest.fixture(params=["memory", "sqlite"])
def store_audit(request, tmp_path):
    """Every approval/gateway test runs against both stores."""
    if request.param == "memory":
        db = Database(":memory:")
        yield InMemoryActionStore(), AuditLog(db)
    else:
        db = Database(tmp_path / "guardrails.db")
        yield SQLiteActionStore(db), AuditLog(db)
    db.close()
