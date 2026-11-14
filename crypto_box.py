"""Passphrase encryption shared by the cloud pipeline and the phone app (WebCrypto speaks the same format).

AES-256-GCM, key = PBKDF2-HMAC-SHA256(passphrase, random 16-byte salt, 200k iterations), random 12-byte IV.
Stored as JSON: {"v": 1, "salt": b64, "iv": b64, "ct": b64}. Files are gzip-compressed before encrypting.
"""
import base64, gzip, json, os
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ITERATIONS = 200_000


def _key(passphrase, salt):
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=ITERATIONS).derive(passphrase.encode())


def encrypt(data: bytes, passphrase: str) -> bytes:
    salt, iv = os.urandom(16), os.urandom(12)
    ct = AESGCM(_key(passphrase, salt)).encrypt(iv, data, None)
    b = lambda x: base64.b64encode(x).decode()
    return json.dumps({"v": 1, "salt": b(salt), "iv": b(iv), "ct": b(ct)}).encode()


def decrypt(blob: bytes, passphrase: str) -> bytes:
    d = json.loads(blob)
    u = base64.b64decode
    return AESGCM(_key(passphrase, u(d["salt"]))).decrypt(u(d["iv"]), u(d["ct"]), None)


def encrypt_file(src, dst, passphrase):
    with open(src, "rb") as f:
        data = gzip.compress(f.read(), 6)
    with open(dst, "wb") as f:
        f.write(encrypt(data, passphrase))


def decrypt_file(src, dst, passphrase):
    with open(src, "rb") as f:
        data = gzip.decompress(decrypt(f.read(), passphrase))
    with open(dst, "wb") as f:
        f.write(data)
