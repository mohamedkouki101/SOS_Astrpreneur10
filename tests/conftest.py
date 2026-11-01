import os
from pathlib import Path

import pytest

DATASET = Path(os.environ.get("DATASET_DIR", Path.home() / "frappe_docker/Data_set/dataset"))


@pytest.fixture(autouse=True)
def isolated_var(tmp_path, monkeypatch):
    """Each test gets its own var/ (audit, outbox, token map) and fresh cached settings."""
    monkeypatch.setenv("AGENTS_VAR_DIR", str(tmp_path / "var"))
    monkeypatch.setenv("DATASET_DIR", str(DATASET))
    monkeypatch.setenv("RHFLOW_PROPOSE_ENABLED", "0")  # tests never write to RH Flow
    monkeypatch.setenv("MAIL_SOURCE", "folder")        # tests read the dataset .eml folder
    monkeypatch.setenv("INPUT_LAYER", "local")         # service-mode tests opt in explicitly
    from agents import config
    from agents.ingest import pipeline

    config.get_settings.cache_clear()
    pipeline.token_store.cache_clear()
    yield
    config.get_settings.cache_clear()
    pipeline.token_store.cache_clear()


needs_dataset = pytest.mark.skipif(not (DATASET / "cvs").is_dir(), reason="synthetic dataset not found (DATASET_DIR)")


class FakeLLM:
    """Scripted stand-in for qwen3:14b: returns the given AIMessages in order."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    def bind_tools(self, tools):
        self.tool_names = [t.name for t in tools]
        return self

    def invoke(self, messages):
        self.seen.append(messages)
        return self.replies.pop(0)
