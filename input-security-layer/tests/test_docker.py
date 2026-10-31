"""Static checks of the container setup (the image itself is built with Docker)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = (ROOT / "Dockerfile").read_text(encoding="utf-8")
DOCKERIGNORE = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
COMPOSE = (ROOT / "compose.ingest.yml").read_text(encoding="utf-8")


def instructions(name: str) -> list[str]:
    return [line.split(None, 1)[1] for line in DOCKERFILE.splitlines() if line.startswith(name + " ")]


def test_runs_as_a_non_root_user():
    users = instructions("USER")
    assert users, "no USER instruction"
    assert users[-1].split(":")[0] not in ("root", "0")
    assert 'user: "10001:10001"' in COMPOSE


def test_only_requirements_and_code_are_copied():
    assert instructions("COPY") == ["requirements.txt .", "ingest/ ingest/"]


def test_no_secrets_in_the_image():
    assert not re.search(r"(KEY|SECRET|PASSWORD)\s*=", DOCKERFILE)
    for entry in (".env", ".venv/", "evidence/", ".git/", "NOTES.md", "CLAUDE.md", ".claude/", "tests/"):
        assert entry in DOCKERIGNORE, entry


def test_prebuilt_packages_only():
    assert "--only-binary=:all:" in DOCKERFILE


def test_compose_hardening():
    for setting in ("read_only: true", "cap_drop: [ALL]", "no-new-privileges:true",
                    "mem_limit:", "cpus:", "pids_limit:", "tmpfs:"):
        assert setting in COMPOSE, setting


def test_compose_joins_frappe_network_and_keeps_secrets_out():
    assert "networks: [frappe_network]" in COMPOSE
    assert re.search(r"frappe_network:\s*\n\s*external: true", COMPOSE)
    assert "env_file: .env" in COMPOSE
    assert not re.search(r"(KEY|SECRET|PASSWORD)\s*[:=]\s*\S", COMPOSE)
    assert '"127.0.0.1:8090:8090"' in COMPOSE  # never published on all interfaces


def test_command_matches_the_api_factory():
    assert '"ingest.api:create_app", "--factory"' in DOCKERFILE
    assert "/health" in DOCKERFILE  # healthcheck


def test_tldextract_never_writes_a_cache():
    """Read-only filesystem: Presidio's tldextract must not try to cache to disk."""
    import tldextract.tldextract as module

    from ingest.pii.mask import _block_tldextract_downloads

    _block_tldextract_downloads()
    assert module.TLD_EXTRACTOR._cache.enabled is False
    assert module.TLD_EXTRACTOR.suffix_list_urls == ()
