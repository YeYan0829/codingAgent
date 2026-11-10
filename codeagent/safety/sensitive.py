from __future__ import annotations

from enum import StrEnum
from pathlib import Path

SENSITIVE_EXACT = {".env", "id_rsa", "id_ed25519"}
ENV_TEMPLATE_NAMES = {".env.example", ".env.sample", ".env.template"}
SENSITIVE_KEYWORDS = ("token", "secret", "password", "credential", "credentials")


class SensitiveListingMode(StrEnum):
    SHOW_MARKED = "SHOW_MARKED"
    REDACT_NAME = "REDACT_NAME"
    HIDE = "HIDE"


def is_sensitive_path(path: Path) -> bool:
    return any(_is_sensitive_name(part) for part in path.parts)


def _is_sensitive_name(value: str) -> bool:
    name = value.lower()
    if name in ENV_TEMPLATE_NAMES:
        return False
    if name in SENSITIVE_EXACT:
        return True
    if name.startswith(".env."):
        return True
    if name.endswith((".pem", ".key")):
        return True
    return any(keyword in name for keyword in SENSITIVE_KEYWORDS)


def sensitive_label(path: Path) -> str | None:
    if not is_sensitive_path(path):
        return None
    name = path.name.lower()
    if any(keyword in name for keyword in SENSITIVE_KEYWORDS):
        return "sensitive-name"
    return "sensitive"
