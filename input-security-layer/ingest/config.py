"""Service configuration, loaded from environment variables (and .env in development).

Design rules:
- Fail closed: a missing or weak secret stops the service at startup.
  There is no default secret to fall back to.
- Secrets are SecretStr: printing or logging the settings shows '**********'.
- Every limit is an integer with a lower and upper bound, so a typo
  cannot silently disable a protection (e.g. MAX_FILE_BYTES=0 or 10**12).
"""

from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MIN_KEY_LENGTH = 32
# The input-security-layer/ folder. .env and a relative EVIDENCE_DIR are found from
# here, not from wherever the command is started (the team repo root, an IDE...).
SERVICE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=SERVICE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="forbid",  # an unknown name in .env (likely a typo) is an error
        frozen=True,  # settings cannot be changed while the service runs
        hide_input_in_errors=True,  # a rejected secret never appears in startup error logs
    )

    # --- HMAC keys ---
    ingest_signing_key: SecretStr  # signs the envelope
    ingest_ref_key: SecretStr  # candidate_ref / sender_ref

    # --- Internal API keys: one per caller ---
    upload_api_key: SecretStr  # Frappe -> upload / email endpoints
    report_api_key: SecretStr  # HR dashboard -> inspection reports (contain PII)
    envelope_api_key: SecretStr  # AI layer -> released envelopes (masked)
    resolver_api_key: SecretStr  # Execution Gateway -> token resolver

    # No mailbox settings: Frappe's Email Account receives HR mail and forwards
    # it to POST /v1/email and /v1/email/attachment. One less secret here.

    # --- Sanitizer limits ---
    max_file_bytes: int = Field(default=10_000_000, ge=1_024, le=50_000_000)
    max_pdf_pages: int = Field(default=20, ge=1, le=100)
    max_zip_entries: int = Field(default=1_000, ge=10, le=10_000)
    max_zip_uncompressed_bytes: int = Field(default=50_000_000, ge=1_000_000, le=200_000_000)
    max_zip_ratio: int = Field(default=100, ge=10, le=1_000)  # uncompressed / compressed
    max_text_chars: int = Field(default=200_000, ge=1_000, le=1_000_000)  # extracted text per file
    extract_timeout_s: int = Field(default=10, ge=1, le=60)
    extract_memory_mb: int = Field(default=256, ge=64, le=1_024)

    # --- Evidence Store ---
    evidence_dir: Path = Path("evidence")  # relative = inside input-security-layer/; Docker sets /data/evidence

    @field_validator("evidence_dir")
    @classmethod
    def anchor_to_service_dir(cls, value: Path) -> Path:
        return value if value.is_absolute() else SERVICE_DIR / value

    @field_validator(
        "ingest_signing_key", "ingest_ref_key", "upload_api_key", "report_api_key",
        "envelope_api_key", "resolver_api_key",
    )
    @classmethod
    def key_is_long_enough(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < MIN_KEY_LENGTH:
            # The error message never includes the key itself.
            raise ValueError(f"must be at least {MIN_KEY_LENGTH} characters")
        return value

    @model_validator(mode="after")
    def keys_are_distinct(self) -> "Settings":
        # One key per purpose: a leaked upload key must not let anyone
        # forge signatures or resolve PII tokens.
        keys = [
            self.ingest_signing_key.get_secret_value(),
            self.ingest_ref_key.get_secret_value(),
            self.upload_api_key.get_secret_value(),
            self.report_api_key.get_secret_value(),
            self.envelope_api_key.get_secret_value(),
            self.resolver_api_key.get_secret_value(),
        ]
        if len(set(keys)) != len(keys):
            raise ValueError("all HMAC and API keys must be different")
        return self


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them for the life of the process."""
    return Settings()
