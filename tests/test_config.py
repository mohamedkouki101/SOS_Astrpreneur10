import pytest
from pydantic import ValidationError

from ingest.config import Settings

# Fictional test values only.
VALID_ENV = {
    "INGEST_SIGNING_KEY": "a" * 64,
    "INGEST_REF_KEY": "b" * 64,
    "UPLOAD_API_KEY": "c" * 64,
    "RESOLVER_API_KEY": "d" * 64,
    "IMAP_USER": "hr-inbox@example.test",
    "IMAP_PASSWORD": "test-password",
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
    assert settings.imap_folder == "INBOX"


@pytest.mark.parametrize("name", ["INGEST_SIGNING_KEY", "INGEST_REF_KEY", "IMAP_PASSWORD"])
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
    for name in ("INGEST_SIGNING_KEY", "INGEST_REF_KEY", "UPLOAD_API_KEY", "RESOLVER_API_KEY"):
        assert VALID_ENV[name] not in text
    assert VALID_ENV["IMAP_PASSWORD"] not in text


def test_secret_not_leaked_in_error_message(env):
    leaked = "short-secret-xyz"
    env.setenv("INGEST_SIGNING_KEY", leaked)
    with pytest.raises(ValidationError) as error:
        load()
    assert leaked not in str(error.value)
