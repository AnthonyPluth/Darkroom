from __future__ import annotations

import os
import re
import shutil
import stat
import zipfile
from pathlib import Path
from typing import Any, Protocol

from darkroom_browser import selectors
from darkroom_browser.config import local_zone
from darkroom_browser.context import Browser, JobContext, JobFailure
from darkroom_browser.jobs.session import SIGN_IN_STATES, ensure_signed_in
from darkroom_browser.jobs.status import open_archive
from darkroom_browser.jobs.telemetry import Clicks
from darkroom_browser.pages import takeout
from darkroom_browser.stall import IN_PROGRESS_SUFFIX

PART_FILE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,195}\.zip$")
PERSON = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
FOLDER = re.compile(r"^export-[0-9]{1,12}$")
ACCOUNT = re.compile(r"^[0-9]{1,12}$")
DOWNLOADS = ".downloads"
HANDLE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
SHARED_FOLDER_MODE = 0o2770
PART_MODE = 0o660
PARTIAL = ".partial"
SIZE_SLACK = 0.06
MIN_SLACK = 1024 * 1024
BACKOFF_SECONDS = 3 * 60.0
ASKED_TO_VERIFY = "asked_to_verify"
MAX_SEEN = 300
UNREADABLE_PAGES = "the browser's pages could not be read"
NO_TABS = "no tab open"


class DownloadFailed(JobFailure):
    pass


class DownloadsFolderRefused(JobFailure):
    pass


def make_shared(path: Path) -> None:
    try:
        path.mkdir()
    except FileExistsError:
        return
    os.chmod(path, SHARED_FOLDER_MODE)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- the configured shared inbox group needs rwx, others get nothing


def share_tree(root: Path) -> int:
    failed = 0
    if root.is_symlink():
        return 1
    for current, _folders, _files in os.walk(root, followlinks=False):
        path = Path(current)
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.lstat().st_mode)
        wanted = mode | stat.S_IRWXG
        if wanted == mode:
            continue
        try:
            os.chmod(path, wanted)  # nosemgrep: python.lang.security.audit.insecure-file-permissions.insecure-file-permissions -- adds only the shared group's bits to folders (g+rwx) so Darkroom can remove the profile; files and others' bits are untouched
        except OSError:
            failed += 1
    return failed


def downloads_folder(profile_dir: Path, working_root: Path) -> Path:
    account = profile_dir.parent.name
    if profile_dir.name != "profile" or not ACCOUNT.match(account):
        raise DownloadsFolderRefused("the browser profile is not an account's profile folder")
    inbox = working_root / "_inbox"
    folder = inbox / DOWNLOADS / account
    for each in (inbox, inbox / DOWNLOADS, folder):
        if each.is_symlink():
            raise DownloadsFolderRefused("Chromium's download folder is a link")
    for each in (inbox, inbox / DOWNLOADS, folder):
        make_shared(each)
    if folder.resolve() != inbox.resolve() / DOWNLOADS / account:
        raise DownloadsFolderRefused("Chromium's download folder is not where it should be")
    return folder


def checked_downloads(folder: Path) -> Path:
    inbox = folder.parent.parent
    if folder.parent.name != DOWNLOADS or not ACCOUNT.match(folder.name) or any(each.is_symlink() for each in (inbox, folder.parent, folder)):
        raise DownloadsFolderRefused("Chromium's download folder is a link or not where it should be")
    if folder.resolve() != inbox.resolve() / DOWNLOADS / folder.name:
        raise DownloadsFolderRefused("Chromium's download folder is not where it should be")
    return folder


def remove_download(folder: Path, handle: str) -> int:
    if not HANDLE.match(handle):
        raise DownloadsFolderRefused("a download's file name is not one Chromium gives")
    checked_downloads(folder)
    removed = 0
    for name in (handle, handle + IN_PROGRESS_SUFFIX):
        own = folder / name
        if own.is_file() and not own.is_symlink():
            own.unlink()
            removed += 1
    return removed


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


def leads_where_checked(browser: Browser, selector: str, checked: str) -> bool:
    try:
        targets = browser.link_targets(selector)
    except Exception:
        return False
    return bool(targets) and set(targets) == {checked}


def part_links(ctx: JobContext, browser: Browser, google_id: str, count: int, needed: int, clicks: Clicks, wait: bool = True) -> dict[int, takeout.ListedPart]:
    try:
        detail = takeout.parse_archive(*open_archive(ctx, browser, takeout.archive_url(google_id), clicks.answered, wait), google_id, needed, zone=local_zone())
    except takeout.UnreadableExports as error:
        raise DownloadFailed(str(error)) from error
    if detail.state != "ready":
        raise DownloadFailed("Takeout no longer lists this export as ready to download")
    links = {part.number: part for part in detail.parts}
    if len(links) + detail.unreadable != count or not set(links) <= set(range(1, count + 1)) or needed not in links:
        raise DownloadFailed(f"Takeout lists {len(links) + detail.unreadable} parts for an export Darkroom expects {count} parts from")
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


def report_failure(ctx: JobContext, number: int, reason: str, seen: str = "") -> None:
    reply = ctx.reporter.part(ctx.job_id, number, {"event": "failed", "reason": reason})
    if reply.get("gave_up"):
        raise DownloadFailed(gave_up_message(number, reply["part"], seen))


def gave_up_message(number: int, part: dict[str, Any], seen: str) -> str:
    attempts, used, most, counted = part["attempts"], part["downloads_used"], part.get("max_downloads"), part.get("sign_in_prompts")
    exact = isinstance(counted, int) and not isinstance(counted, bool) and 0 <= counted <= used
    prompts = counted if exact else max(0, used - attempts)
    if isinstance(most, int) and used >= most:
        if exact:
            said = f"part {number} used all {used} of Google's downloads for it: {used - prompts} that did not finish or that Google counted elsewhere and {prompts} that ended at Google's sign-in prompt"
        else:
            said = f"part {number} used all {used} of Google's downloads for it: {attempts} failed attempts and {prompts} that ended at Google's sign-in prompt or that Google counted elsewhere"
    else:
        limit = f"Google's {most}" if isinstance(most, int) else "Google's"
        said = f"part {number} failed after {attempts} attempts, using {used} of {limit} downloads"
        elsewhere = "" if exact else " or Google counted elsewhere"
        said += f" ({prompts} ended at Google's sign-in prompt{elsewhere})" if prompts else ""
    led_to = f"; the last click led to: {seen}" if seen else ""
    return f"{said}; Darkroom stopped before using more{led_to}"


def what_the_click_led_to(browser: Browser) -> tuple[str, str]:
    try:
        tabs = browser.tabs()
    except Exception:
        return takeout.UNKNOWN, UNREADABLE_PAGES
    if not tabs:
        return takeout.UNKNOWN, NO_TABS
    read = [(takeout.classify_at(tab.page, tab.address), tab) for tab in tabs]
    state, shown = next(((state, tab) for state, tab in read if state in SIGN_IN_STATES), read[0])
    return state, takeout.click_findings(shown.page, shown.address, len(tabs), shown.primary)[:MAX_SEEN]


class PartsRunner(Protocol):
    def __call__(self, ctx: JobContext, browser: Browser, folder: Path, plan: dict[str, Any], parts: list[dict[str, Any]], taken: set[str], clicks: Clicks) -> int: ...


def download_export(ctx: JobContext, run_parts: PartsRunner) -> dict[str, object]:
    plan = ctx.reporter.download_plan(ctx.job_id)
    folder = inbox_folder(ctx.photos_root, plan)
    parts = checked_parts(plan)
    taken = {str(part["filename"]) for part in parts if part.get("status") == "done" and part.get("filename")}
    for part in parts:
        if part.get("status") == "done" and not (folder / str(part.get("filename"))).is_file():
            raise DownloadFailed(f"part {part['number']} is recorded as downloaded but is not in the inbox")
    browser = ctx.browser()
    clicks = Clicks(ctx)
    if ensure_signed_in(ctx, browser):
        clicks.answered()
    fetched = run_parts(ctx, browser, folder, plan, parts, taken, clicks)
    discard_partial(folder)
    ctx.progress(f"downloaded {fetched} of {len(parts)} parts; {clicks.summary()}")
    return {"export_id": plan.get("export_id"), "parts": len(parts), "downloaded": fetched, "telemetry": clicks.result()}
