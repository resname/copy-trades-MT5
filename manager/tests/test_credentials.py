# manager/tests/test_credentials.py
import base64
import pytest

from manager.settings import credentials


class FakeCrypto:
    """Mimics win32crypt: CryptProtectData(data, desc, None, None, None, 0) -> bytes;
    CryptUnprotectData(blob, None, None, None, 0) -> (desc, bytes)."""
    def __init__(self):
        self.calls = []

    def CryptProtectData(self, data, desc, *rest):
        self.calls.append(("protect", data, desc))
        return b"ENC:" + data

    def CryptUnprotectData(self, blob, *rest):
        self.calls.append(("unprotect", blob))
        if not blob.startswith(b"ENC:"):
            raise ValueError("bad blob")
        return ("CopyTradesMT5", blob[len(b"ENC:"):])


def test_encrypt_decrypt_round_trip():
    crypto = FakeCrypto()
    blob = credentials.encrypt("s3cret", crypto=crypto)
    assert blob == b"ENC:s3cret"
    assert credentials.decrypt(blob, crypto=crypto) == "s3cret"


def test_encrypt_password_is_base64_string():
    crypto = FakeCrypto()
    blob_str = credentials.encrypt_password("s3cret", crypto=crypto)
    assert isinstance(blob_str, str)
    # base64-decodes back to the raw DPAPI blob
    assert base64.b64decode(blob_str) == b"ENC:s3cret"
    assert credentials.decrypt_password(blob_str, crypto=crypto) == "s3cret"


def test_decrypt_garbage_raises_credential_decrypt_error():
    crypto = FakeCrypto()
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt(b"not-a-real-blob", crypto=crypto)


def test_decrypt_non_bytes_raises_credential_decrypt_error():
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt("string-not-bytes", crypto=FakeCrypto())


def test_decrypt_password_bad_base64_raises_credential_decrypt_error():
    with pytest.raises(credentials.CredentialDecryptError):
        credentials.decrypt_password("!!!not base64!!!", crypto=FakeCrypto())


def test_encrypt_rejects_none():
    with pytest.raises(ValueError):
        credentials.encrypt(None, crypto=FakeCrypto())