"""Production secret-posture checks (Phase 4 / ops baseline).

Pure, dependency-free so it can be unit-tested and reused. The startup hook in
``main.py`` always logs any problems and, when ``deployment_mode == "production"``,
refuses to start — so a real deployment can never run on the shipped default
``ADMIN_TOKEN`` / ``UPDATE_SECRET`` or an unset ``ENCRYPTION_KEY``.
"""

from __future__ import annotations

from shared.database_credentials import database_credential_problems
from shared.runtime_secrets import encryption_key_problem

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
    problem = encryption_key_problem(value)
    return {
        "encryption_key_missing": "ENCRYPTION_KEY is not set",
        "encryption_key_invalid_base64": "ENCRYPTION_KEY is not valid base64",
        "encryption_key_wrong_length": "ENCRYPTION_KEY must decode to 32 bytes",
    }.get(problem)


def secret_posture(
    *,
    admin_token: str | None,
    update_secret: str | None,
    encryption_key: str | None,
    database_url: str | None = None,
    database_runtime_user: str | None = None,
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
    for issue in database_credential_problems(
        database_url,
        role="runtime",
        expected_username=database_runtime_user,
    ):
        problems.append({"key": "DATABASE_URL", "issue": issue})
    return problems


def format_posture_problems(problems: list[dict]) -> str:
    return "; ".join(f"{item['key']} ({item['issue']})" for item in problems)
