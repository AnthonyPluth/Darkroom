from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


class Unreadable(Exception):
    pass


def fernet(secret: str) -> Fernet:
    if not secret:
        raise Unreadable("DARKROOM_SECRET_KEY is not set")
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


def seal(plain: str, secret: str) -> str:
    return PREFIX + fernet(secret).encrypt(plain.encode()).decode()


def is_sealed(value: str) -> bool:
    return value.startswith(PREFIX)


def unseal(sealed: str, secret: str) -> str:
    if not is_sealed(sealed):
        raise Unreadable("the value is not sealed")
    try:
        return fernet(secret).decrypt(sealed[len(PREFIX):].encode()).decode()
    except InvalidToken as error:
        raise Unreadable("the value cannot be read with this key") from error
