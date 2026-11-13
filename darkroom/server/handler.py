from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError

from darkroom import monitoring
from darkroom.config import Settings, no_auth_allowed
from darkroom.server import auth, routes, static
from darkroom.server.common import CSP, Principal, Request, Response, error_response
from darkroom.storage import db

Authenticator = Callable[[Request], Principal | None]

MAX_BODY = 8 * 1024 * 1024

AUTHENTICATORS: dict[str, Authenticator] = {"session": auth.session_principal, "worker": auth.worker_principal}


def authenticate(route: routes.Route, request: Request) -> Principal | None:
    check = AUTHENTICATORS.get(route.auth)
    return check(request) if check is not None else None


def dispatch(request: Request) -> Response:
    found = routes.match(request.method, request.path)
    if found is None:
        if request.method == "GET" and not request.path.startswith(("/api/", "/internal/")):
            return static.serve(request.path, vnc_url=request.settings.vnc_url)
        return error_response("not found", 404)
    route, params = found
    request = replace(request, params=params)
    if route.auth == "session" and no_auth_allowed(request.settings) and not auth.no_auth_address_allowed(request):
        monitoring.log("request refused without sign-in", reason="host not allowed")
        return error_response(auth.HOST_REFUSED, 403)
    if route.auth == "session" and request.method != "GET" and not auth.same_origin(request):
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
                          self.rfile.read(length) if length else b"", self.settings, self.engine)
        self.answer(safe_dispatch(request))

    def answer(self, response: Response) -> None:
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
