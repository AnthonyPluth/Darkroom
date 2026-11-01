from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

TIMEOUT = 30.0
MAX_REPLY = 1024 * 1024


class Unreachable(Exception):
    pass


class Refused(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"darkroom answered {status}: {message}")
        self.status = status


class LeaseLost(Refused):
    pass


class NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: Any, msg: Any, headers: Any, newurl: Any) -> Any:
        raise Unreachable("darkroom redirected; the worker follows no redirects")


class Client:
    def __init__(self, base_url: str, token: str, timeout: float = TIMEOUT) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirects())

    def post(self, path: str, body: dict[str, object]) -> dict[str, Any]:
        request = urllib.request.Request(self.base_url + path, data=json.dumps(body).encode(), method="POST",
                                         headers={"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(MAX_REPLY)
        except urllib.error.HTTPError as error:
            with error:
                detail = error.read(MAX_REPLY)
            message = self.error_text(detail)
            if error.code in (404, 409):
                raise LeaseLost(error.code, message) from None
            raise Refused(error.code, message) from None
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise Unreachable(type(error).__name__) from None
        parsed = json.loads(raw or b"{}")
        if not isinstance(parsed, dict):
            raise Unreachable("darkroom answered something other than a JSON object")
        return parsed

    @staticmethod
    def error_text(raw: bytes) -> str:
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            return "unreadable error"
        return str(parsed.get("error", "unknown error")) if isinstance(parsed, dict) else "unknown error"

    def claim(self, worker: str) -> dict[str, Any] | None:
        job = self.post("/internal/browser/jobs/claim", {"worker": worker}).get("job")
        return job if isinstance(job, dict) else None

    def heartbeat(self, job_id: int) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/heartbeat", {})

    def progress(self, job_id: int, message: str) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/progress", {"message": message})

    def waiting(self, job_id: int, reason: str) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/waiting", {"reason": reason})

    def done(self, job_id: int, result: dict[str, object]) -> None:
        self.post(f"/internal/browser/jobs/{job_id}/done", {"result": result})

    def failed(self, job_id: int, message: str, failure_dir: str | None, files: list[str] | None = None) -> None:
        body: dict[str, object] = {"message": message}
        if failure_dir is not None:
            body["failure_dir"] = failure_dir
            body["files"] = list(files or [])
        self.post(f"/internal/browser/jobs/{job_id}/failed", body)
