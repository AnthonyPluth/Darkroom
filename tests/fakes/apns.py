from __future__ import annotations

import datetime
import ipaddress
import json
import socket
import ssl
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

HOST = "127.0.0.1"


@dataclass(frozen=True)
class Received:
    path: str
    headers: dict[str, str]
    body: bytes
    claims: dict[str, object] | None
    key_id: str


@dataclass
class State:
    public_key: ec.EllipticCurvePublicKey
    replies: dict[str, tuple[int, str]] = field(default_factory=dict)
    unregistered_at: dict[str, object] = field(default_factory=dict)
    close_after_handshake: bool = False
    closed_after_handshake: threading.Event = field(default_factory=threading.Event)
    expire_tokens: int = 0
    drop_after: int | None = None
    alpn: str = "h2"
    received: list[Received] = field(default_factory=list)
    connections: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


def new_key() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def pem_of(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def write_certificate(folder: Path) -> tuple[Path, Path]:
    key = new_key()
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "fake apns")])
    now = datetime.datetime.now(datetime.UTC)
    public = key.public_key()
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(public).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(days=1)).not_valid_after(now + datetime.timedelta(days=1))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=False, content_commitment=False, key_encipherment=False,
                                                data_encipherment=False, key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST)), x509.DNSName("localhost")]), critical=False)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(public), critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(public), critical=False)
                   .sign(key, hashes.SHA256()))
    cert_file, key_file = folder / "fake-apns.crt", folder / "fake-apns.key"
    cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(pem_of(key))
    return cert_file, key_file


def claims_of(state: State, headers: dict[str, str]) -> tuple[dict[str, object] | None, str]:
    bearer = headers.get("authorization", "")
    if not bearer.startswith("bearer "):
        return None, ""
    token = bearer[len("bearer "):]
    try:
        key_id = str(jwt.get_unverified_header(token).get("kid", ""))
        return jwt.decode(token, state.public_key, algorithms=["ES256"], options={"verify_exp": False, "verify_iat": False}), key_id
    except jwt.InvalidTokenError:
        return None, ""


def answer(state: State, received: Received) -> tuple[int, str]:
    if received.claims is None:
        return 403, "InvalidProviderToken"
    with state.lock:
        if state.expire_tokens:
            state.expire_tokens -= 1
            return 403, "ExpiredProviderToken"
        token = received.path.rsplit("/", 1)[-1]
        return state.replies.get(token, (200, ""))


def serve_connection(state: State, sock: ssl.SSLSocket) -> None:
    connection = h2.connection.H2Connection(h2.config.H2Configuration(client_side=False, header_encoding="utf-8"))
    heads: dict[int, dict[str, str]] = {}
    bodies: dict[int, bytearray] = {}
    served = 0
    try:
        connection.initiate_connection()
        sock.sendall(connection.data_to_send())
        while True:
            data = sock.recv(65535)
            if not data:
                return
            for event in connection.receive_data(data):
                if isinstance(event, h2.events.RequestReceived):
                    heads[event.stream_id] = {str(name): str(value) for name, value in event.headers or []}
                    bodies[event.stream_id] = bytearray()
                elif isinstance(event, h2.events.DataReceived):
                    bodies[event.stream_id] += event.data or b""
                    connection.acknowledge_received_data(event.flow_controlled_length or 0, event.stream_id)
                elif isinstance(event, h2.events.StreamEnded):
                    headers = heads.pop(event.stream_id)
                    claims, key_id = claims_of(state, headers)
                    received = Received(headers.get(":path", ""), headers, bytes(bodies.pop(event.stream_id)), claims, key_id)
                    with state.lock:
                        state.received.append(received)
                        drop = state.drop_after is not None and served >= state.drop_after
                    if drop:
                        sock.close()
                        return
                    status, reason = answer(state, received)
                    stamp = state.unregistered_at.get(received.path.rsplit("/", 1)[-1])
                    body = json.dumps({"reason": reason} | ({"timestamp": stamp} if stamp is not None else {})).encode() if reason else b""
                    reply = [(":status", str(status)), ("apns-id", f"fake-{len(state.received)}")]
                    connection.send_headers(event.stream_id, reply, end_stream=not body)
                    if body:
                        connection.send_data(event.stream_id, body, end_stream=True)
                    served += 1
            sock.sendall(connection.data_to_send())
    except (OSError, h2.exceptions.ProtocolError):
        return
    finally:
        sock.close()


class FakeApns:
    def __init__(self, folder: Path, public_key: ec.EllipticCurvePublicKey) -> None:
        self.state = State(public_key)
        self.cafile, key_file = write_certificate(folder)
        self.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self.context.load_cert_chain(self.cafile, key_file)
        self.listener = socket.create_server((HOST, 0))
        self.port = self.listener.getsockname()[1]
        self.thread = threading.Thread(target=self.accept, daemon=True)

    def __enter__(self) -> FakeApns:
        self.context.set_alpn_protocols([self.state.alpn])
        self.thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.listener.close()

    def accept(self) -> None:
        while True:
            try:
                raw, _ = self.listener.accept()
            except OSError:
                return
            with self.state.lock:
                self.state.connections += 1
            try:
                sock = self.context.wrap_socket(raw, server_side=True)
            except (OSError, ssl.SSLError):
                raw.close()
                continue
            if self.state.close_after_handshake:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                sock.close()
                self.state.closed_after_handshake.set()
                continue
            threading.Thread(target=serve_connection, args=(self.state, sock), daemon=True).start()
