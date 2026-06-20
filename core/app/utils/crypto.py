import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


# AES-GCM Additional Authenticated Data (P2-2): binds a ciphertext to its
# purpose so a blob encrypted for one use cannot be silently decrypted for
# another (e.g. moving a notification-secret ciphertext into the account
# credential column, or vice versa). The AAD is authenticated but not secret.
CREDENTIAL_AAD = "dpms:account-credential"


def notification_secret_aad(key_name: str) -> str:
    """Per-key AAD for a notification secret, e.g. ``FEISHU_WEBHOOK``."""
    return f"dpms:notification-secret:{key_name}"


def _aad_bytes(aad: str | bytes | None) -> bytes | None:
    if aad is None:
        return None
    return aad.encode("utf-8") if isinstance(aad, str) else aad


class CookieVault:
    def __init__(self):
        key = base64.b64decode(settings.encryption_key)
        if len(key) != 32:
            raise ValueError("ENCRYPTION_KEY must be 32 bytes base64 encoded")
        self._aesgcm = AESGCM(key)

    def encrypt(self, plaintext: str, *, aad: str | bytes | None = None) -> bytes:
        nonce = os.urandom(12)
        ct = self._aesgcm.encrypt(nonce, plaintext.encode("utf-8"), _aad_bytes(aad))
        return nonce + ct

    def decrypt(self, ciphertext: bytes | str, *, aad: str | bytes | None = None) -> str:
        if isinstance(ciphertext, str):
            ciphertext = ciphertext.encode("utf-8")
        nonce, ct = ciphertext[:12], ciphertext[12:]
        aad_b = _aad_bytes(aad)
        try:
            return self._aesgcm.decrypt(nonce, ct, aad_b).decode()
        except InvalidTag:
            # Backward compatibility: ciphertext written before AAD binding has
            # no AAD. Fall back to an unbound decrypt so existing credentials and
            # secrets keep working; re-encryption migrates them to bound form.
            if aad_b is None:
                raise
            return self._aesgcm.decrypt(nonce, ct, None).decode()


class LazyCookieVault:
    """Import-safe proxy for CookieVault.

    The old module-level ``cookie_vault = CookieVault()`` decoded
    ENCRYPTION_KEY during import, before startup secret-posture checks could
    report a controlled error. This proxy keeps existing call sites
    (``cookie_vault.encrypt/decrypt``) while constructing the vault only when a
    credential operation actually needs it.
    """

    def __init__(self):
        self._vault: CookieVault | None = None

    def _get(self) -> CookieVault:
        if self._vault is None:
            self._vault = CookieVault()
        return self._vault

    def encrypt(self, *args, **kwargs):
        return self._get().encrypt(*args, **kwargs)

    def decrypt(self, *args, **kwargs):
        return self._get().decrypt(*args, **kwargs)


cookie_vault = LazyCookieVault()
