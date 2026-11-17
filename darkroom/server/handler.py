from __future__ import annotations

import os
import socketserver
import stat
from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from darkroom import config, monitoring, tls
from darkroom.config import Settings, no_auth_allowed
from darkroom.server import auth, routes, static, wizard
from darkroom.server.common import CSP, Principal, Request, Response, asks_for_websocket, error_response, header, viewer_url
from darkroom.storage import db, stored_settings

Authenticator = Callable[[Request], Principal | None]

MAX_BODY = 8 * 1024 * 1024

AUTHENTICATORS: dict[str, Authenticator] = {"session": auth.session_principal, "worker": auth.worker_principal,
                                             "setup": wizard.principal, "setup-claim": wizard.claim_principal}
BROWSER_KINDS = ("session", *routes.SETUP_KINDS)
WITHOUT_STORED = ("/healthz",)


def with_stored(request: Request) -> Settings:
    if request.engine is None:
        return request.settings
    try:
        return stored_settings.effective(request.settings, request.engine).settings
    except OperationalError as error:
        if db.is_busy(error):
            raise
        monitoring.log("stored settings not read; the request uses the settings Darkroom started with", error=type(error).__name__)
        return request.settings


def authenticate(route: routes.Route, request: Request) -> Principal | None:
    check = AUTHENTICATORS.get(route.auth)
    return check(request) if check is not None else None


def on_its_listener(found: tuple[routes.Route, dict[str, str]] | None, request: Request) -> bool:
    for_the_worker = found is not None and found[0].auth == "worker"
    return for_the_worker == request.worker_socket


def dispatch(request: Request) -> Response:
    found = routes.match(request.method, request.path)
    if not on_its_listener(found, request):
        return error_response("not found", 404)
    if found is None:
        if request.method == "GET" and not request.path.startswith(("/api/", "/internal/")):
            return static.serve(request.path, vnc_url=viewer_url(request.settings) or "")
        return error_response("not found", 404)
    route, params = found
    request = replace(request, params=params, settings=request.settings if route.path in WITHOUT_STORED else with_stored(request))
    if route.auth in BROWSER_KINDS and no_auth_allowed(request.settings) and not auth.no_auth_address_allowed(request):
        monitoring.log("request refused without sign-in", reason="host not allowed")
        return error_response(auth.HOST_REFUSED, 403)
    if route.auth in BROWSER_KINDS and request.method != "GET" and not auth.same_origin(request):
        return error_response("cross-site requests are refused", 403)
    if route.auth in BROWSER_KINDS and asks_for_websocket(request) and (header(request, "Origin") is None or not auth.same_origin(request)):
        return error_response("cross-site requests are refused", 403)
    if route.auth == "public":
        return route.handler(request)
    principal = authenticate(route, request)
    if principal is None:
        return error_response("sign in required", 401)
    response = route.handler(replace(request, user=principal))
    if principal.renewed and not sets_session_cookie(response):
        token = auth.cookies_of(request).get(auth.SESSION_COOKIE, "")
        response = replace(response, cookies=(*response.cookies, auth.session_cookie(request.settings, token, principal.renewed)))
    return response


BUSY_MESSAGE = "Darkroom is busy with another change; try again in a moment"


def sets_session_cookie(response: Response) -> bool:
    return any(baked.startswith(auth.SESSION_COOKIE + "=") for baked in response.cookies)


def line_breaks_in(response: Response) -> bool:
    lines = [*response.headers.keys(), *response.headers.values(), *response.cookies, response.content_type]
    return any("\r" in line or "\n" in line for line in lines)


def safe_dispatch(request: Request) -> Response:
    try:
        response = dispatch(request)
    except OperationalError as error:
        if not db.is_busy(error):
            monitoring.report()
            return error_response("something went wrong", 500)
        monitoring.log("request postponed: the database was busy", path=request.path)
        return error_response(BUSY_MESSAGE, 503)
    except Exception:
        monitoring.report()
        return error_response("something went wrong", 500)
    if line_breaks_in(response):
        monitoring.log("response header refused", path=request.path, reason="line break")
        return error_response("something went wrong", 500)
    return response


class Handler(BaseHTTPRequestHandler):
    settings: Settings
    engine: Engine | None = None
    worker_socket = False

    def peer(self) -> str:
        return str(self.client_address[0])

    def handle_any(self) -> None:
        parts = urlsplit(self.path)
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.reject(400 if length < 0 else 413)
            return
        request = Request(self.command, parts.path, parse_qs(parts.query), dict(self.headers.items()),
                          self.rfile.read(length) if length else b"", self.settings, self.engine, client=self.peer(), target=self.path,
                          worker_socket=self.worker_socket)
        self.answer(safe_dispatch(request))

    def answer(self, response: Response) -> None:
        if response.takeover is not None:
            self.close_connection = True
            response.takeover(self.connection, self.rfile)
            return
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", response.headers.get("Content-Security-Policy", CSP))
        for name, value in response.headers.items():
            if name != "Content-Security-Policy":
                self.send_header(name, value)
        for baked in response.cookies:
            self.send_header("Set-Cookie", baked)
        self.end_headers()
        self.wfile.write(response.body)

    def reject(self, status: int) -> None:
        response = error_response("bad request" if status == 400 else "request too large", status)
        self.close_connection = True
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(response.body)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_any

    def send_header(self, keyword: str, value: str) -> None:
        if "\r" in f"{keyword}{value}" or "\n" in f"{keyword}{value}":
            raise ValueError("a response header cannot contain a line break")
        super().send_header(keyword, value)

    def log_message(self, format: str, *args: object) -> None:
        monitoring.log("request", method=self.command, status=str(args[1]) if len(args) > 1 else "")


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        monitoring.report()


def make_server(settings: Settings, host: str | None = None, port: int | None = None, engine: Engine | None = None) -> ThreadingHTTPServer:
    bound = type("BoundHandler", (Handler,), {"settings": settings, "engine": engine or db.make_engine(db.database_url(settings))})
    return Server((host or settings.host, settings.port if port is None else port), bound)


SOCKET_MODE = 0o600


class SocketRefused(Exception):
    pass


class WorkerHandler(Handler):
    worker_socket = True

    def peer(self) -> str:
        return ""


class WorkerSocketServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: str, handler: type[WorkerHandler]) -> None:
        self.socket_path = socket_path
        super().__init__(socket_path, handler)

    def server_bind(self) -> None:
        super().server_bind()
        os.chmod(self.socket_path, SOCKET_MODE)

    def handle_error(self, request: object, client_address: object) -> None:
        monitoring.report()


def clear_stale_socket(socket_path: str) -> None:
    try:
        found = os.lstat(socket_path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(found.st_mode):
        raise SocketRefused("something other than a socket is where the worker's socket goes")
    os.unlink(socket_path)


def make_worker_server(settings: Settings, socket_path: Path | None = None, engine: Engine | None = None) -> WorkerSocketServer:
    try:
        checked = tls.private_socket(str(config.WORKER_SOCKET if socket_path is None else socket_path))
    except tls.Refused as refused:
        raise SocketRefused(str(refused)) from None
    clear_stale_socket(checked)
    bound = type("BoundWorkerHandler", (WorkerHandler,), {"settings": settings, "engine": engine or db.make_engine(db.database_url(settings))})
    return WorkerSocketServer(checked, bound)
