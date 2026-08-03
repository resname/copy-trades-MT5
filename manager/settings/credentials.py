# manager/settings/credentials.py
from __future__ import annotations

import base64


class CredentialDecryptError(Exception):
    """Raised when a stored credential blob cannot be decrypted — wrong user,
    wrong machine, corrupted, or tampered. The GUI catches this and prompts
    the user to re-enter the credential. DPAPI blobs are bound to the OS user
    that encrypted them; an admin password reset (vs. a user self-change) also
    invalidates the master keys and makes prior blobs unrecoverable."""


def _load_crypto():
    """Lazy-import win32crypt so the test suite runs without pywin32."""
    import win32crypt  # pywin32
    return win32crypt


def encrypt(plaintext: str, crypto=None) -> bytes:
    """DPAPI-encrypt a UTF-8 plaintext string under the current user's
    OS-managed key (Flags=0). Returns the opaque blob bytes. ``crypto`` is
    the win32crypt module, injected in tests; lazy-imported when None."""
    if plaintext is None:
        raise ValueError("plaintext must be a str")
    if not isinstance(plaintext, str):
        raise ValueError("plaintext must be a str")
    mod = crypto if crypto is not None else _load_crypto()
    return mod.CryptProtectData(plaintext.encode("utf-8"), "CopyTradesMT5",
                                None, None, None, 0)


def decrypt(blob, crypto=None) -> str:
    """Inverse of encrypt. Raises CredentialDecryptError on any failure
    (pywintypes.error from a cross-user/machine blob, corrupted blob, wrong
    type, etc.). The broad `except` is intentional: pywintypes.error is not
    importable without pywin32, and any decrypt failure means re-prompt."""
    if not isinstance(blob, (bytes, bytearray)):
        raise CredentialDecryptError("blob must be bytes")
    mod = crypto if crypto is not None else _load_crypto()
    try:
        _desc, data = mod.CryptUnprotectData(bytes(blob), None, None, None, 0)
    except CredentialDecryptError:
        raise
    except Exception as exc:
        raise CredentialDecryptError(str(exc)) from exc
    return data.decode("utf-8")


def encrypt_password(plaintext: str, crypto=None) -> str:
    """JSON-safe credential: base64 of the DPAPI blob. Store this string in
    the account dict under ``password_blob``; it survives JSON round-trips
    and never exposes the plaintext or the raw blob in logs."""
    return base64.b64encode(encrypt(plaintext, crypto=crypto)).decode("ascii")


def decrypt_password(blob: str, crypto=None) -> str:
    """Inverse of encrypt_password."""
    if not isinstance(blob, str):
        raise CredentialDecryptError("password blob must be a str")
    try:
        raw = base64.b64decode(blob, validate=True)
    except Exception as exc:
        raise CredentialDecryptError(str(exc)) from exc
    return decrypt(raw, crypto=crypto)