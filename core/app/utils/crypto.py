
import os, base64

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings



class CookieVault:

    def __init__(self):

        key = base64.b64decode(settings.encryption_key)

        if len(key) != 32:

            raise ValueError("ENCRYPTION_KEY must be 32 bytes base64 encoded")

        self._aesgcm = AESGCM(key)



    def encrypt(self, plaintext: str) -> bytes:

        nonce = os.urandom(12)

        ct = self._aesgcm.encrypt(nonce, plaintext.encode(), None)

        return nonce + ct



    def decrypt(self, ciphertext: bytes) -> str:

        nonce, ct = ciphertext[:12], ciphertext[12:]

        return self._aesgcm.decrypt(nonce, ct, None).decode()



cookie_vault = CookieVault()
