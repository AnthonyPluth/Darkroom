from __future__ import annotations

import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

from darkroom_browser import selectors
from darkroom_browser.context import Browser, Cancelled, DownloadInterrupted, JobContext, JobFailure
from darkroom_browser.jobs.session import POLL_SECONDS, SIGN_IN_STATES, WAIT_LIMIT_SECONDS, SignInTimedOut, ensure_signed_in
from darkroom_browser.jobs.status import open_exports
from darkroom_browser.pages import takeout

PART_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,195}\.zip$")
PERSON = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FOLDER = re.compile(r"^export-[0-9]{1,12}$")
ACCOUNT = re.compile(r"^[0-9]{1,12}$")
DOWNLOADS = ".downloads"
SHARED_FOLDER_MODE = 0o2770
PART_MODE = 0o660
PARTIAL = ".partial"
SIZE_SLACK = 0.06
MIN_SLACK = 1024 * 1024


class DownloadFailed(JobFailure):
    pass


class DownloadsFolderRefused(JobFailure):
    pass


def make_shared(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        return
    os.chmod(path, SHARED_FOLDER_MODE)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the shared inbox group (gid 10010) needs rwx, others get nothing


def downloads_folder(profile_dir: Path) -> Path:
    account = profile_dir.parent.name
    if profile_dir.name != "profile" or not ACCOUNT.match(account):
        raise DownloadsFolderRefused("the browser profile is not an account's profile folder")
    inbox = profile_dir.parent.parent.parent / "_inbox"
    folder = inbox / DOWNLOADS / account
    for each in (inbox, inbox / DOWNLOADS, folder):
        if each.is_symlink():
            raise DownloadsFolderRefused("Chromium's download folder is a link")
    for each in (inbox, inbox / DOWNLOADS, folder):
        make_shared(each)
    if folder.resolve() != inbox.resolve() / DOWNLOADS / account:
        raise DownloadsFolderRefused("Chromium's download folder is not where it should be")
    return folder


def sweep_downloads(folder: Path) -> int:
    removed = 0
    for leftover in folder.iterdir():
        if leftover.is_symlink() or leftover.is_file():
            leftover.unlink()
            removed += 1
    return removed


def text_of(plan: dict[str, Any], name: str, pattern: re.Pattern[str]) -> str:
    value = plan.get(name)
    if not isinstance(value, str) or not pattern.match(value):
        raise DownloadFailed("the download plan names a folder outside the inbox")
    return value


def inbox_folder(root: Path, plan: dict[str, Any]) -> Path:
    inbox = root / "_inbox"
    person = inbox / text_of(plan, "person", PERSON)
    folder = person / text_of(plan, "folder", FOLDER)
    for each in (inbox, person, folder):
        if each.is_symlink():
            raise DownloadFailed("the export's inbox folder is a link")
    for each in (inbox, person, folder):
        make_shared(each)
    if not folder.resolve().is_relative_to(inbox.resolve()):
        raise DownloadFailed("the export's inbox folder is outside the inbox")
    return folder


def fresh_partial(folder: Path) -> Path:
    partial = folder / PARTIAL
    if partial.is_symlink() or partial.is_file():
        partial.unlink()
    elif partial.exists():
        shutil.rmtree(partial)
    make_shared(partial)
    return partial


def discard_partial(folder: Path) -> None:
    fresh_partial(folder).rmdir()


def checked_parts(plan: dict[str, Any]) -> list[dict[str, Any]]:
    parts = plan.get("parts")
    count = plan.get("part_count")
    if not isinstance(parts, list) or not isinstance(count, int) or count < 1:
        raise DownloadFailed("the download plan lists no parts")
    numbers = sorted(part["number"] for part in parts if isinstance(part, dict) and isinstance(part.get("number"), int))
    if numbers != list(range(1, count + 1)):
        raise DownloadFailed(f"the export should have {count} parts but Darkroom lists {len(numbers)}")
    return sorted(parts, key=lambda part: part["number"])


def part_link(href: str) -> str:
    return selectors.PART_LINK_TEMPLATE.format(href=href.replace("\\", "\\\\").replace('"', '\\"'))


def part_links(ctx: JobContext, browser: Browser, google_id: str, count: int) -> dict[int, str]:
    try:
        listings = takeout.parse_exports(open_exports(ctx, browser))
    except takeout.UnreadableExports as error:
        raise DownloadFailed(str(error)) from error
    found = [listing for listing in listings if listing.google_id == google_id]
    if not found or found[0].state != "ready":
        raise DownloadFailed("Takeout no longer lists this export as ready to download")
    links = {part.number: part.href for part in found[0].parts}
    if sorted(links) != list(range(1, count + 1)):
        raise DownloadFailed(f"Takeout lists {len(links)} parts for an export Darkroom expects {count} parts from")
    return links


def size_fits(listed: object, actual: int) -> bool:
    if actual <= 0:
        return False
    if not isinstance(listed, int):
        return True
    return abs(actual - listed) <= max(listed * SIZE_SLACK, MIN_SLACK)


def problem_with(path: Path, listed: object, taken: set[str]) -> str | None:
    if not PART_FILE.match(path.name) or path.name in taken or path.is_symlink() or not path.is_file():
        return "bad_name"
    if not size_fits(listed, path.stat().st_size):
        return "wrong_size"
    try:
        with zipfile.ZipFile(path) as archive:
            return "corrupt" if archive.testzip() is not None else None
    except (zipfile.BadZipFile, OSError, EOFError):
        return "corrupt"


def report_failure(ctx: JobContext, number: int, reason: str) -> None:
    reply = ctx.reporter.part(ctx.job_id, number, {"event": "failed", "reason": reason})
    if reply.get("gave_up"):
        raise DownloadFailed(f"part {number} failed after {reply['part']['attempts']} attempts; Darkroom stopped before using more of Google's downloads")


def bytes_reporter(ctx: JobContext, number: int) -> Any:
    def report(size: int) -> None:
        try:
            ctx.reporter.part(ctx.job_id, number, {"event": "progress", "bytes": size})
        except Exception:
            return

    return report


def wait_for_verification(ctx: JobContext, browser: Browser, number: int) -> None:
    ctx.wait_for_user("verify")
    started = ctx.clock()
    while takeout.classify(browser.content()) in SIGN_IN_STATES:
        if ctx.clock() - started > WAIT_LIMIT_SECONDS:
            raise SignInTimedOut("nobody confirmed it was them within 48 hours")
        ctx.sleep(POLL_SECONDS)
        ctx.check()
    ensure_signed_in(ctx, browser)
    ctx.progress(f"verified; resuming part {number}")


def fetch_part(ctx: JobContext, browser: Browser, folder: Path, plan: dict[str, Any], part: dict[str, Any], taken: set[str]) -> str:
    number = part["number"]
    while True:
        href = part_links(ctx, browser, str(plan.get("google_id", "")), plan["part_count"])[number]
        reply = ctx.reporter.part(ctx.job_id, number, {"event": "attempt"})
        if not reply.get("allowed"):
            raise DownloadFailed(f"part {number} has used its attempts or Google's downloads; Darkroom stopped before using more")
        partial = fresh_partial(folder)
        ctx.progress(f"downloading part {number} of {plan['part_count']}")
        try:
            path = browser.download_to(part_link(href), partial, bytes_reporter(ctx, number))
        except Cancelled:
            raise
        except Exception as error:
            asked = takeout.classify(browser.content()) in SIGN_IN_STATES
            report_failure(ctx, number, "interrupted" if isinstance(error, DownloadInterrupted) else "not_started")
            if asked:
                wait_for_verification(ctx, browser, number)
            continue
        ctx.check()
        problem = problem_with(path, part.get("bytes"), taken)
        if problem is not None:
            report_failure(ctx, number, problem)
            continue
        size = path.stat().st_size
        os.replace(path, folder / path.name)
        os.chmod(folder / path.name, PART_MODE)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- Darkroom reads parts through the shared group, others get nothing
        ctx.reporter.part(ctx.job_id, number, {"event": "done", "filename": path.name, "bytes": size})
        return path.name


def download_export(ctx: JobContext) -> dict[str, object]:
    plan = ctx.reporter.download_plan(ctx.job_id)
    folder = inbox_folder(ctx.photos_root, plan)
    parts = checked_parts(plan)
    taken = {str(part["filename"]) for part in parts if part.get("status") == "done" and part.get("filename")}
    for part in parts:
        if part.get("status") == "done" and not (folder / str(part.get("filename"))).is_file():
            raise DownloadFailed(f"part {part['number']} is recorded as downloaded but is not in the inbox")
    browser = ctx.browser()
    ensure_signed_in(ctx, browser)
    fetched = 0
    for part in parts:
        if part.get("status") == "done":
            continue
        taken.add(fetch_part(ctx, browser, folder, plan, part, taken))
        fetched += 1
    discard_partial(folder)
    ctx.progress(f"downloaded {fetched} of {len(parts)} parts")
    return {"export_id": plan.get("export_id"), "parts": len(parts), "downloaded": fetched}
