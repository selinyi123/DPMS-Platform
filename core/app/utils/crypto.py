import base64
import binascii
import hashlib
import hmac
import ipaddress
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


# AES-GCM Additional Authenticated Data (P2-2): binds a ciphertext to its
# purpose so a blob encrypted for one use cannot be silently decrypted for
# another (e.g. moving a notification-secret ciphertext into the account
# credential column, or vice versa). The AAD is authenticated but not secret.
CREDENTIAL_AAD = "dpms:account-credential"
WEIBO_RIP_AAD = "dpms:weibo-rip:v1"
WEIBO_RIP_HMAC_CONTEXT = b"dpms:weibo-rip-hmac:v1"


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

    def decrypt_strict(self, ciphertext: bytes | str, *, aad: str | bytes) -> str:
        """Decrypt an AAD-bound value without the credential legacy fallback."""

        if isinstance(ciphertext, str):
            ciphertext = ciphertext.encode("utf-8")
        nonce, ct = ciphertext[:12], ciphertext[12:]
        return self._aesgcm.decrypt(nonce, ct, _aad_bytes(aad)).decode()


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

    def decrypt_strict(self, *args, **kwargs):
        return self._get().decrypt_strict(*args, **kwargs)


cookie_vault = LazyCookieVault()


def encrypt_weibo_rip(value: str | None) -> str:
    """Seal a canonical Weibo ``rip`` for durable queue transport."""

    plaintext = str(value or "")
    if not plaintext:
        return ""
    ciphertext = cookie_vault.encrypt(plaintext, aad=WEIBO_RIP_AAD)
    return base64.urlsafe_b64encode(ciphertext).decode("ascii")


def decrypt_weibo_rip(value: str | bytes | None) -> str:
    """Open a queue ``rip`` envelope with strict purpose binding."""

    if value in (None, "", b""):
        return ""
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError as exc:
            raise ValueError("weibo_rip_encrypted_invalid") from exc
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 1 <= len(value) <= 256
    ):
        raise ValueError("weibo_rip_encrypted_invalid")
    try:
        ciphertext = base64.b64decode(
            value.encode("ascii"), altchars=b"-_", validate=True
        )
        if base64.urlsafe_b64encode(ciphertext).decode("ascii") != value:
            raise ValueError("weibo_rip_encrypted_invalid")
        return cookie_vault.decrypt_strict(ciphertext, aad=WEIBO_RIP_AAD)
    except (binascii.Error, InvalidTag, UnicodeError, ValueError) as exc:
        raise ValueError("weibo_rip_encrypted_invalid") from exc


def weibo_rip_hmac(value: str | None) -> str:
    """Return a keyed, non-reversible binding for a canonical Weibo ``rip``."""

    canonical_ip = str(value or "")
    if not canonical_ip:
        return ""
    try:
        parsed_ip = ipaddress.ip_address(canonical_ip)
        if not parsed_ip.is_global or parsed_ip.compressed != canonical_ip:
            raise ValueError("weibo_rip_hmac_input_invalid")
        master_key = base64.b64decode(settings.encryption_key, validate=True)
        message = canonical_ip.encode("ascii")
    except (binascii.Error, TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValueError("weibo_rip_hmac_input_invalid") from exc
    if len(master_key) != 32:
        raise ValueError("ENCRYPTION_KEY must be 32 bytes base64 encoded")
    derived_key = hmac.new(
        master_key,
        WEIBO_RIP_HMAC_CONTEXT,
        hashlib.sha256,
    ).digest()
    return hmac.new(derived_key, message, hashlib.sha256).hexdigest()
