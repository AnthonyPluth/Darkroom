from __future__ import annotations

import ssl
import urllib.request
from collections.abc import Mapping
from http.client import HTTPException, HTTPResponse
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 15.0


class Refused(Exception):
    pass


CONNECTION_PROBLEMS: tuple[type[Exception], ...] = (OSError, HTTPException)


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Refused("redirects are not followed")


def client_context(cafile: str | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


def urlopen(url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None,
            method: str | None = None, timeout: float = DEFAULT_TIMEOUT, allow_http: bool = False) -> HTTPResponse:
    scheme = urlsplit(url).scheme
    if scheme != "https" and not (scheme == "http" and allow_http):
        raise Refused("only https addresses are allowed here")
    request = urllib.request.Request(url, data=data, headers=dict(headers or {}), method=method)
    context = client_context()
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=context), NoRedirects())
    response = opener.open(request, timeout=timeout)
    assert isinstance(response, HTTPResponse)
    return response

