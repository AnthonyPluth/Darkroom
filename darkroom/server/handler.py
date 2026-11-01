from __future__ import annotations

from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from darkroom import monitoring
from darkroom.config import Settings
from darkroom.server import routes, static
from darkroom.server.common import CSP, Request, Response, error_response

Authenticator = Callable[[Request], bool]

AUTHENTICATORS: dict[str, Authenticator] = {}


def allowed(route: routes.Route, request: Request) -> bool:
    if route.auth == "public":
        return True
    check = AUTHENTICATORS.get(route.auth)
    return check is not None and check(request)


def dispatch(request: Request) -> Response:
    route = routes.find(request.method, request.path)
    if route is None:
        if request.method == "GET" and not request.path.startswith("/api/"):
            return static.serve(request.path)
        return error_response("not found", 404)
    if not allowed(route, request):
        return error_response("sign in required", 401)
    return route.handler(request)


class Handler(BaseHTTPRequestHandler):
    settings: Settings

    def handle_any(self) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        request = Request(self.command, parts.path, parse_qs(parts.query), dict(self.headers.items()),
                          self.rfile.read(length) if length else b"", self.settings)
        response = dispatch(request)
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", response.headers.get("Content-Security-Policy", CSP))
        for name, value in response.headers.items():
            if name != "Content-Security-Policy":
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = handle_any

    def log_message(self, format: str, *args: object) -> None:
        monitoring.log("request", method=self.command, status=str(args[1]) if len(args) > 1 else "")


def make_server(settings: Settings, host: str | None = None, port: int | None = None) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"settings": settings})
    server = ThreadingHTTPServer((host or settings.host, settings.port if port is None else port), handler)
    server.daemon_threads = True
    return server
