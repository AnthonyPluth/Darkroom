from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from darkroom.server import auth
from darkroom.server.api import accounts, browser, browser_internal, cleanup, device, devices, exports, imports, people, pixels, review, status
from darkroom.server.common import Request, Response

AUTH_KINDS = ("session", "device", "worker", "public")


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    handler: Callable[[Request], Response]
    auth: str
    streams: bool = False


ROUTES: tuple[Route, ...] = (
    Route("GET", "/healthz", status.healthz, "public"),
    Route("GET", "/auth/login", auth.login, "public"),
    Route("GET", "/auth/callback", auth.callback, "public"),
    Route("POST", "/auth/logout", auth.logout, "session"),
    Route("GET", "/auth/signed-out", auth.signed_out, "public"),
    Route("GET", "/auth/device/start", auth.device_start, "public"),
    Route("GET", "/api/me", auth.me, "session"),
    Route("GET", "/api/status", status.status, "session"),
    Route("GET", "/api/events", status.recent_events, "session"),
    Route("GET", "/api/people", people.list_people, "session"),
    Route("POST", "/api/people", people.create_person, "session"),
    Route("PATCH", "/api/people/{id}", people.update_person, "session"),
    Route("DELETE", "/api/people/{id}", people.delete_person, "session"),
    Route("GET", "/api/people/{id}/devices", people.list_person_devices, "session"),
    Route("DELETE", "/api/devices/{id}", devices.revoke_device, "session"),
    Route("PATCH", "/api/devices/{id}", devices.update_device, "session"),
    Route("GET", "/api/accounts", accounts.list_accounts, "session"),
    Route("POST", "/api/accounts", accounts.create_account, "session"),
    Route("PATCH", "/api/accounts/{id}", accounts.update_account, "session"),
    Route("DELETE", "/api/accounts/{id}", accounts.delete_account, "session"),
    Route("POST", "/api/accounts/{id}/connect", accounts.connect, "session"),
    Route("POST", "/api/accounts/{id}/schedule", accounts.create_schedule, "session"),
    Route("POST", "/api/accounts/{id}/schedule/checked", accounts.schedule_checked, "session"),
    Route("POST", "/api/accounts/{id}/schedule/renew", accounts.renew_schedule, "session"),
    Route("GET", "/api/pixels", pixels.list_pixels, "session"),
    Route("POST", "/api/pixels", pixels.create_pixel, "session"),
    Route("PATCH", "/api/pixels/{id}", pixels.update_pixel, "session"),
    Route("DELETE", "/api/pixels/{id}", pixels.delete_pixel, "session"),
    Route("GET", "/api/browser/jobs", browser.list_jobs, "session"),
    Route("POST", "/api/browser/jobs", browser.enqueue, "session"),
    Route("GET", "/api/browser/view", browser.view, "session"),
    Route("GET", "/api/browser/failures/{id}/screenshot.png", browser.screenshot, "session"),
    Route("GET", "/api/browser/failures/{id}/page.html", browser.page_snapshot, "session"),
    Route("GET", "/api/exports", exports.list_exports, "session"),
    Route("POST", "/api/exports/check-now", exports.check_now, "session"),
    Route("GET", "/api/exports/{id}", exports.export_one, "session"),
    Route("POST", "/api/exports/{id}/import", imports.request, "session"),
    Route("GET", "/api/imports", imports.list_imports, "session"),
    Route("GET", "/api/imports/{id}", imports.import_one, "session"),
    Route("GET", "/api/review", review.list_reviews, "session"),
    Route("GET", "/api/review/{id}", review.review_one, "session"),
    Route("GET", "/api/review/{id}/thumb/{side}", review.thumb, "session"),
    Route("POST", "/api/review/{id}/resolve", review.resolve, "session"),
    Route("POST", "/api/review/{id}/undo", review.undo, "session"),
    Route("GET", "/api/decisions", review.list_decisions, "session"),
    Route("POST", "/api/decisions/{id}/reverse", review.reverse, "session"),
    Route("GET", "/api/media", review.list_media, "session"),
    Route("POST", "/api/media/{id}/reassign", review.reassign, "session"),
    Route("POST", "/api/device/assets/check", device.check_assets, "device"),
    Route("PUT", "/api/device/assets/{id}/resources/{kind}", device.upload_resource, "device", streams=True),
    Route("GET", "/api/device/me", device.device_me, "device"),
    Route("POST", "/api/device/checkin", device.checkin, "device"),
    Route("PUT", "/api/device/push-token", device.register_push_token, "device"),
    Route("DELETE", "/api/device/push-token", device.forget_push_token, "device"),
    Route("POST", "/api/device/eligible", cleanup.eligible, "device"),
    Route("POST", "/api/device/eligible/confirm", cleanup.confirm, "device"),
    Route("POST", "/api/device/deleted", cleanup.deleted, "device"),
    Route("POST", "/internal/browser/jobs/claim", browser_internal.claim, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/heartbeat", browser_internal.heartbeat, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/progress", browser_internal.progress, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/waiting", browser_internal.waiting, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/done", browser_internal.done, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/failed", browser_internal.failed, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/download", browser_internal.download_plan, "worker"),
    Route("POST", "/internal/browser/jobs/{id}/parts/{number}", browser_internal.part, "worker"),
)


def segments_match(pattern: str, path: str) -> dict[str, str] | None:
    wanted, given = pattern.split("/"), path.split("/")
    if len(wanted) != len(given):
        return None
    params: dict[str, str] = {}
    for expected, actual in zip(wanted, given, strict=True):
        if expected.startswith("{") and expected.endswith("}"):
            if not actual:
                return None
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


def match(method: str, path: str) -> tuple[Route, dict[str, str]] | None:
    for route in ROUTES:
        if route.method != method:
            continue
        params = segments_match(route.path, path)
        if params is not None:
            return route, params
    return None


def find(method: str, path: str) -> Route | None:
    found = match(method, path)
    return found[0] if found else None
