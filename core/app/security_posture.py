"""Production secret-posture checks (Phase 4 / ops baseline).

Pure, dependency-free so it can be unit-tested and reused. The startup hook in
``main.py`` always logs any problems and, when ``deployment_mode == "production"``,
refuses to start — so a real deployment can never run on the shipped default
``ADMIN_TOKEN`` / ``UPDATE_SECRET`` or an unset ``ENCRYPTION_KEY``.
"""

from __future__ import annotations

import base64

# The values shipped in app.config as defaults. Running with these in production
# means anyone who read the repo holds the admin token / update-signing secret.
DEFAULT_ADMIN_TOKEN = "change-me-admin-token"
DEFAULT_UPDATE_SECRET = "changeme"
MIN_SECRET_LENGTH = 16


def _weak_token(value: str | None, default: str) -> bool:
    if not value:
        return True
    if value == default:
        return True
    return len(value) < MIN_SECRET_LENGTH


def _encryption_key_problem(value: str | None) -> str | None:
    if not value:
        return "ENCRYPTION_KEY is not set"
    try:
        raw = base64.b64decode(value, validate=True)
    except Exception:
        return "ENCRYPTION_KEY is not valid base64"
    if len(raw) != 32:
        return "ENCRYPTION_KEY must decode to 32 bytes"
    return None


def secret_posture(
    *,
    admin_token: str | None,
    update_secret: str | None,
    encryption_key: str | None,
    database_url: str | None = None,
) -> list[dict]:
    """Return a list of secret-posture problems, each ``{key, issue}``.

    Empty list means the posture is acceptable for production.
    """
    problems: list[dict] = []
    if _weak_token(admin_token, DEFAULT_ADMIN_TOKEN):
        problems.append({"key": "ADMIN_TOKEN", "issue": f"default or shorter than {MIN_SECRET_LENGTH} chars"})
    if _weak_token(update_secret, DEFAULT_UPDATE_SECRET):
        problems.append({"key": "UPDATE_SECRET", "issue": f"default or shorter than {MIN_SECRET_LENGTH} chars"})
    enc_problem = _encryption_key_problem(encryption_key)
    if enc_problem:
        problems.append({"key": "ENCRYPTION_KEY", "issue": enc_problem})
    if database_url and ("user:password@" in database_url or ":password@" in database_url):
        problems.append({"key": "DATABASE_URL", "issue": "uses the default database password"})
    return problems


def format_posture_problems(problems: list[dict]) -> str:
    return "; ".join(f"{item['key']} ({item['issue']})" for item in problems)
