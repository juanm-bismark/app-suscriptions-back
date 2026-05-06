"""Fernet-based credential encryption/decryption.

Used by the tenancy layer to store and load provider credentials.
The Fernet key must be a 32-byte URL-safe base64-encoded string (generate with
`Fernet.generate_key().decode()`).
"""

import json
from typing import Any, Mapping, Dict

from cryptography.fernet import Fernet, InvalidToken

from app.shared.errors import ProviderUnavailable


def encrypt_credentials(plaintext: Mapping[str, Any], fernet_key: str) -> str:
    f = Fernet(fernet_key.encode())
    return f.encrypt(json.dumps(plaintext).encode()).decode()


def decrypt_credentials(encrypted: str, fernet_key: str) -> Dict[str, Any]:
    try:
        f = Fernet(fernet_key.encode())
        return json.loads(f.decrypt(encrypted.encode()))
    except InvalidToken as exc:
        raise ProviderUnavailable(
            detail="Credential decryption failed — verify FERNET_KEY configuration"
        ) from exc
    except Exception as exc:
        raise ProviderUnavailable(detail=f"Credential load error: {exc}") from exc
