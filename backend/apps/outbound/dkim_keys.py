"Public API."
import base64
import hashlib

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from django.conf import settings

RSA_KEY_SIZE = 2048


def _fernet() -> Fernet:
    # Fernet requires a 32-byte urlsafe-base64 key. Derive one deterministically
    # from the configured master key so rotating OUTBOUND_DKIM_MASTER_KEY
    # deterministically invalidates every previously-encrypted private key
    # (mirrors "rotating it invalidates every stored DKIM private key" in
    # settings base.py's comment on OUTBOUND_DKIM_MASTER_KEY).
    digest = hashlib.sha256(settings.OUTBOUND_DKIM_MASTER_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def generate_keypair() -> tuple[bytes, str]:
    """Generate a fresh RSA-2048 keypair.

    Returns ``(private_key_pem, dkim_public_key_txt)`` where the second
    element is the exact value to publish as the DKIM TXT record
    (``v=DKIM1; k=rsa; p=<base64 pubkey, no headers>``).
    """
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=RSA_KEY_SIZE)
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_der = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_b64 = base64.b64encode(public_der).decode("ascii")
    txt_value = f"v=DKIM1; k=rsa; p={public_b64}"
    return private_pem, txt_value


def encrypt_private_key(private_key_pem: bytes) -> bytes:
    return _fernet().encrypt(private_key_pem)


def decrypt_private_key(encrypted: bytes) -> bytes:
    return _fernet().decrypt(bytes(encrypted))
