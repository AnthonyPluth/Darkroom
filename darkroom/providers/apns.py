from __future__ import annotations

import json
import re
import stat
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import jwt
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from darkroom import config, monitoring, tls

PRODUCTION_HOST = "api.push.apple.com"
PORT = 443
TIMEOUT = 15.0
TOKEN_LIFETIME = 45 * 60.0
EXPIRES_AFTER = 24 * 3600
MAX_KEY_FILE = 16 * 1024
MAX_PAYLOAD = 4096
MAX_TIMESTAMP_MS = 10**14
DEVICE_TOKEN = re.compile(r"[0-9a-f]{64,200}")
INVALID_TOKEN_REASONS = frozenset({"BadDeviceToken", "DeviceTokenNotForTopic", "Unregistered", "ExpiredToken"})

Outcome = Literal["sent", "invalid_token", "failed"]


class KeyUnusable(Exception):
    pass


@dataclass(frozen=True)
class Push:
    kind: str
    alert_id: int
    title: str


@dataclass(frozen=True)
class Result:
    outcome: Outcome
    reason: str
    unregistered_at: float | None = None


class Pusher(Protocol):
    def send(self, tokens: Sequence[str], push: Push) -> list[Result]: ...


@dataclass(frozen=True)
class Credentials:
    key_id: str
    team_id: str
    bundle_id: str
    key_file: Path


def credentials_of(settings: config.Settings) -> Credentials | None:
    if not config.apns_configured(settings) or config.apns_problems(settings):
        return None
    return Credentials(settings.apns_key_id, settings.apns_team_id, settings.apns_bundle_id, Path(settings.apns_key_file))


def load_key(path: Path) -> ec.EllipticCurvePrivateKey:
    try:
        if path.is_symlink() or not path.is_file():
            raise KeyUnusable("APNS_KEY_FILE is not a file")
        if stat.S_IMODE(path.stat().st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
            raise KeyUnusable("APNS_KEY_FILE must be readable only by its owner (chmod 600)")
        with path.open("rb") as handle:
            raw = handle.read(MAX_KEY_FILE + 1)
    except OSError:
        raise KeyUnusable("APNS_KEY_FILE cannot be read") from None
    if len(raw) > MAX_KEY_FILE:
        raise KeyUnusable("APNS_KEY_FILE is too large to be an APNs key")
    try:
        key = load_pem_private_key(raw, password=None)
    except (ValueError, TypeError):
        raise KeyUnusable("APNS_KEY_FILE is not an unencrypted APNs auth key (.p8)") from None
    if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
        raise KeyUnusable("APNS_KEY_FILE is not an APNs auth key (it must be a P-256 key)")
    return key


def key_problems(settings: config.Settings) -> list[str]:
    if not config.apns_configured(settings) or config.apns_problems(settings):
        return []
    try:
        load_key(Path(settings.apns_key_file))
    except KeyUnusable as error:
        return [str(error)]
    return []


@dataclass
class Signer:
    credentials: Credentials
    key: ec.EllipticCurvePrivateKey | None = None
    token: str = field(default="", repr=False)
    issued: float = 0.0

    def bearer(self, now: float) -> str:
        if self.token and 0 <= now - self.issued < TOKEN_LIFETIME:
            return self.token
        if self.key is None:
            self.key = load_key(self.credentials.key_file)
        self.token = jwt.encode({"iss": self.credentials.team_id, "iat": int(now)}, self.key, algorithm="ES256",
                                headers={"kid": self.credentials.key_id})
        self.issued = now
        return self.token

    def forget(self) -> None:
        self.token, self.issued = "", 0.0


def payload_of(push: Push) -> bytes:
    body = json.dumps({"aps": {"alert": {"title": push.title}, "sound": "default", "thread-id": push.kind},
                       "kind": push.kind, "alert_id": push.alert_id}, separators=(",", ":")).encode()
    if len(body) > MAX_PAYLOAD:
        raise ValueError("the push payload is too large")
    return body


def reply_of(body: bytes) -> tuple[str, float | None]:
    try:
        found = json.loads(body)
    except ValueError:
        return "", None
    if not isinstance(found, dict):
        return "", None
    reason, stamp = found.get("reason"), found.get("timestamp")
    reason = reason if isinstance(reason, str) and reason.isascii() and reason.isalnum() and len(reason) <= 64 else ""
    at = stamp / 1000 if isinstance(stamp, int) and not isinstance(stamp, bool) and 0 < stamp < MAX_TIMESTAMP_MS else None
    return reason, at


@dataclass
class Apns:
    credentials: Credentials
    clock: Callable[[], float]
    host: str = PRODUCTION_HOST
    port: int = PORT
    cafile: str | None = None
    timeout: float = TIMEOUT
    signer: Signer = field(init=False)

    def __post_init__(self) -> None:
        self.signer = Signer(self.credentials)

    def headers(self, push: Push, now: float) -> dict[str, str]:
        base = {"authorization": f"bearer {self.signer.bearer(now)}", "apns-topic": self.credentials.bundle_id, "content-type": "application/json"}
        return base | {"apns-push-type": "alert", "apns-priority": "10", "apns-expiration": str(int(now) + EXPIRES_AFTER),
                       "apns-collapse-id": f"alert-{push.alert_id}"}

    def send(self, tokens: Sequence[str], push: Push) -> list[Result]:
        results: list[Result | None] = [None if DEVICE_TOKEN.fullmatch(token) else Result("invalid_token", "BadDeviceToken") for token in tokens]
        if all(results):
            return [result for result in results if result is not None]
        body = payload_of(push)
        try:
            with tls.Http2(self.host, self.port, timeout=self.timeout, cafile=self.cafile) as connection:
                for index, token in enumerate(tokens):
                    if results[index] is None:
                        results[index] = self.one(connection, token, push, body)
        except KeyUnusable:
            monitoring.log("apns_key_unusable")
            return [result or Result("failed", "KeyUnusable") for result in results]
        except tls.CONNECTION_PROBLEMS as error:
            monitoring.log("apns_unreachable", error=type(error).__name__)
            return [result or Result("failed", "Unreachable") for result in results]
        return [result or Result("failed", "Unreachable") for result in results]

    def one(self, connection: tls.Http2, token: str, push: Push, body: bytes) -> Result:
        reply = connection.post(f"/3/device/{token}", self.headers(push, self.clock()), body)
        reason, unregistered_at = reply_of(reply.body)
        if reply.status == 403 and reason == "ExpiredProviderToken":
            self.signer.forget()
            reply = connection.post(f"/3/device/{token}", self.headers(push, self.clock()), body)
            reason, unregistered_at = reply_of(reply.body)
        if reply.status == 200:
            return Result("sent", "")
        if reply.status in (400, 410) and reason in INVALID_TOKEN_REASONS:
            return Result("invalid_token", reason, unregistered_at if reply.status == 410 else None)
        return Result("failed", reason or f"HTTP {reply.status}")


def of(settings: config.Settings, clock: Callable[[], float]) -> Apns | None:
    found = credentials_of(settings)
    return Apns(found, clock) if found is not None else None
