import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


CREDENTIAL_AAD = "dpms:account-credential"


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
            if aad_b is None:
                raise
            return self._aesgcm.decrypt(nonce, ct, None).decode()

    def decrypt_strict(self, ciphertext: bytes, *, aad: str | bytes) -> str:
        """Decrypt purpose-bound data without the legacy no-AAD fallback."""

        if not isinstance(ciphertext, bytes) or len(ciphertext) < 29:
            raise ValueError("ciphertext_invalid")
        nonce, ct = ciphertext[:12], ciphertext[12:]
        return self._aesgcm.decrypt(nonce, ct, _aad_bytes(aad)).decode("utf-8")


class LazyCookieVault:
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

    def decrypt_strict(self, *args, **kwargs):
        return self._get().decrypt_strict(*args, **kwargs)


cookie_vault = LazyCookieVault()
