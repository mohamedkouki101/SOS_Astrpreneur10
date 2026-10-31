import pytest
from pydantic import ValidationError

from ingest.config import Settings

# Fictional test values only.
VALID_ENV = {
    "INGEST_SIGNING_KEY": "a" * 64,
    "INGEST_REF_KEY": "b" * 64,
    "UPLOAD_API_KEY": "c" * 64,
    "RESOLVER_API_KEY": "d" * 64,
    "REPORT_API_KEY": "e" * 64,
    "ENVELOPE_API_KEY": "f" * 64,
}


@pytest.fixture
def env(monkeypatch):
    """Set a valid environment; tests change one value to break it."""
    for name, value in VALID_ENV.items():
        monkeypatch.setenv(name, value)
    return monkeypatch


def load() -> Settings:
    # _env_file=None: ignore any local .env so tests only see what they set.
    return Settings(_env_file=None)


def test_valid_env_loads_with_defaults(env):
    settings = load()
    assert settings.max_file_bytes == 10_000_000
    assert settings.max_pdf_pages == 20


@pytest.mark.parametrize("name", ["INGEST_SIGNING_KEY", "INGEST_REF_KEY", "UPLOAD_API_KEY"])
def test_missing_secret_fails(env, name):
    env.delenv(name)
    with pytest.raises(ValidationError):
        load()


def test_short_key_fails(env):
    env.setenv("INGEST_SIGNING_KEY", "too-short")
    with pytest.raises(ValidationError):
        load()


def test_reused_key_fails(env):
    env.setenv("INGEST_REF_KEY", VALID_ENV["INGEST_SIGNING_KEY"])
    with pytest.raises(ValidationError):
        load()


@pytest.mark.parametrize("value", ["0", "999999999999", "10.5", "abc"])
def test_out_of_range_or_non_integer_limit_fails(env, value):
    env.setenv("MAX_FILE_BYTES", value)
    with pytest.raises(ValidationError):
        load()


def test_secrets_not_shown_in_repr(env):
    text = repr(load())
    for name in ("INGEST_SIGNING_KEY", "INGEST_REF_KEY", "UPLOAD_API_KEY", "RESOLVER_API_KEY",
                 "REPORT_API_KEY", "ENVELOPE_API_KEY"):
        assert VALID_ENV[name] not in text



def test_env_file_and_evidence_are_found_from_the_service_folder(env, monkeypatch, tmp_path):
    from ingest.config import SERVICE_DIR

    assert SERVICE_DIR.name == "ingest-service"
    assert Settings.model_config["env_file"] == SERVICE_DIR / ".env"
    monkeypatch.chdir(tmp_path)  # started from somewhere else, e.g. the team repo root
    monkeypatch.setenv("EVIDENCE_DIR", "./evidence")
    assert load().evidence_dir == SERVICE_DIR / "evidence"
    monkeypatch.setenv("EVIDENCE_DIR", str(tmp_path / "abs"))
    assert load().evidence_dir == tmp_path / "abs"  # absolute paths (Docker) are kept


def test_no_mailbox_settings_any_more(env, tmp_path):
    # Mail arrives through Frappe: a leftover IMAP line in .env is refused, not silently ignored.
    assert not any(name.startswith("imap") for name in Settings.model_fields)
    leftover = tmp_path / ".env"
    leftover.write_text("IMAP_PASSWORD=old-secret\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        Settings(_env_file=leftover)


def test_secret_not_leaked_in_error_message(env):
    leaked = "short-secret-xyz"
    env.setenv("INGEST_SIGNING_KEY", leaked)
    with pytest.raises(ValidationError) as error:
        load()
    assert leaked not in str(error.value)
