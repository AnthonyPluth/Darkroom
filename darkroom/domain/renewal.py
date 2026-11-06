from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from darkroom.config import Settings
from darkroom.domain import alerts, browser_jobs, events
from darkroom.storage.models import BrowserJob, GoogleAccount

NOTICE = 21 * alerts.DAY
ENDING = "schedule_ending"
RENEWED = "schedule_renewed"
RENEWAL_FAILED = "schedule_renewal_failed"
RENEWAL_UNCONFIRMED = "schedule_renewal_unconfirmed"


def ending_key(account_id: int) -> str:
    return f"schedule:ending:{account_id}"


def failed_key(account_id: int) -> str:
    return f"schedule:renewal-failed:{account_id}"


def renewed_key(account_id: int, end: float) -> str:
    return f"schedule:renewed:{account_id}:{int(end)}"


def on_date(moment: float, zone: str) -> str:
    local = datetime.fromtimestamp(moment, ZoneInfo(zone))
    return f"{local.day} {local:%b %Y}"


def latest_renewal(opened: Session, account: GoogleAccount, since: float) -> BrowserJob | None:
    return opened.execute(
        select(BrowserJob).where(BrowserJob.account_id == account.id, BrowserJob.kind == browser_jobs.RENEW_KIND, BrowserJob.created >= since).order_by(BrowserJob.id.desc()).limit(1)
    ).scalar_one_or_none()


def ending_link(settings: Settings) -> str | None:
    return f"{settings.public_url}/exports" if settings.public_url else None


def open_failed(opened: Session, settings: Settings, account: GoogleAccount, end: float, job: BrowserJob, now: float) -> None:
    alerts.close(opened, ending_key(account.id), now)
    day = on_date(end, settings.timezone)
    if browser_jobs.left_uncertain(job):
        files = browser_jobs.captured_files(settings, job, browser_jobs.CAPTURE_FILES) if job.failure_dir is not None else []
        link = browser_jobs.capture_link(settings, job, files) or ending_link(settings)
        alerts.close_other_kinds(opened, failed_key(account.id), RENEWAL_UNCONFIRMED, now)
        alerts.open_alert(opened, settings, failed_key(account.id), RENEWAL_UNCONFIRMED, account.email, alerts.schedule_renewal_unconfirmed(account.email, day), link, now, about=alerts.account(account.id))
        return
    alerts.close_other_kinds(opened, failed_key(account.id), RENEWAL_FAILED, now)
    wording = alerts.schedule_renewal_failed(account.email, day, now >= end)
    alert = alerts.open_alert(opened, settings, failed_key(account.id), RENEWAL_FAILED, account.email, wording, ending_link(settings), now, about=alerts.account(account.id))
    alert.message = wording.message


class NoSchedule(ValueError):
    pass


class NeedsSignIn(ValueError):
    pass


class NotDue(ValueError):
    pass


def queue_renewal(opened: Session, account: GoogleAccount, end: float, now: float) -> BrowserJob:
    return browser_jobs.queue(opened, account.id, browser_jobs.RENEW_KIND, {"email": account.email, "ending": end}, now)


def renew_now(opened: Session, settings: Settings, account_id: int, now: float) -> BrowserJob:
    account = browser_jobs.lock_account(opened, account_id)
    end = account.schedule_end
    if account.schedule_created is None or end is None:
        raise NoSchedule("this account has no export schedule to renew")
    if now < end - NOTICE:
        raise NotDue(f"this schedule is not due for renewal until {on_date(end - NOTICE, settings.timezone)}, three weeks before it ends")
    if account.session_state == "needs_sign_in":
        raise NeedsSignIn("this account needs to sign in to Google again before its schedule can be renewed")
    if browser_jobs.unchecked_schedule(opened, account_id) is not None:
        raise browser_jobs.AlreadyOpen("the last schedule attempt may have created an export; check Takeout and say so before renewing")
    if browser_jobs.schedule_attempt_pending(opened, account_id):
        raise browser_jobs.AlreadyOpen("a schedule or renew job for this account is already waiting or running")
    job = queue_renewal(opened, account, end, now)
    alerts.close(opened, failed_key(account.id), now)
    events.record(opened, now, "schedule_renewal_requested", f"account {account.id}", f"the schedule ends {on_date(end, settings.timezone)}")
    return job


def watch_schedules(opened: Session, settings: Settings, now: float) -> int:
    queued = 0
    ending = select(GoogleAccount).where(GoogleAccount.schedule_end.is_not(None)).order_by(GoogleAccount.id)
    for account in opened.execute(ending).scalars().all():
        end = account.schedule_end
        assert end is not None
        if now < end - NOTICE:
            continue
        asked = latest_renewal(opened, account, end - NOTICE)
        if asked is not None and asked.status == "failed":
            open_failed(opened, settings, account, end, asked, now)
            continue
        if asked is None and browser_jobs.schedule_attempt_pending(opened, account.id):
            continue
        wording = alerts.schedule_ending(account.email, on_date(end, settings.timezone))
        alerts.open_alert(opened, settings, ending_key(account.id), ENDING, account.email, wording, ending_link(settings), now, about=alerts.account(account.id))
        if asked is not None:
            continue
        queue_renewal(opened, account, end, now)
        events.record(opened, now, "schedule_renewal_queued", f"account {account.id}", f"the schedule ends {on_date(end, settings.timezone)}")
        queued += 1
    return queued


def job_failed(opened: Session, settings: Settings, job: BrowserJob, now: float) -> None:
    if job.kind != browser_jobs.RENEW_KIND:
        return
    account = browser_jobs.account_of(opened, job.account_id)
    if account.schedule_end is None:
        return
    open_failed(opened, settings, account, account.schedule_end, job, now)
    kind = "schedule_renewal_unconfirmed" if browser_jobs.left_uncertain(job) else "schedule_renewal_failed"
    events.record(opened, now, kind, f"account {account.id}", f"the schedule still ends {on_date(account.schedule_end, settings.timezone)}")


def jobs_failed(opened: Session, settings: Settings, jobs: Sequence[BrowserJob], now: float) -> None:
    for job in jobs:
        job_failed(opened, settings, job, now)


def settle(opened: Session, account_id: int, now: float) -> None:
    alerts.close(opened, ending_key(account_id), now)
    alerts.close(opened, failed_key(account_id), now)


def schedule_recorded(opened: Session, settings: Settings, job: BrowserJob, now: float) -> None:
    if job.kind == browser_jobs.RENEW_KIND:
        renewed(opened, settings, job, now)
    elif job.kind == "schedule":
        settle(opened, job.account_id, now)


def checked(opened: Session, settings: Settings, account_id: int, found: bool, now: float) -> BrowserJob:
    job = browser_jobs.mark_schedule_checked(opened, account_id, now, found)
    if found:
        schedule_recorded(opened, settings, job, now)
        return job
    account = browser_jobs.account_of(opened, account_id)
    if job.kind == browser_jobs.RENEW_KIND and account.schedule_end is not None:
        open_failed(opened, settings, account, account.schedule_end, job, now)
        events.record(opened, now, "schedule_renewal_failed", f"account {account.id}", f"not found in Takeout; the schedule still ends {on_date(account.schedule_end, settings.timezone)}")
    return job


def renewed(opened: Session, settings: Settings, job: BrowserJob, now: float) -> None:
    account = browser_jobs.account_of(opened, job.account_id)
    end = account.schedule_end
    assert end is not None
    settle(opened, account.id, now)
    wording = alerts.schedule_renewed(account.email, on_date(end, settings.timezone))
    alerts.open_alert(opened, settings, renewed_key(account.id, end), RENEWED, account.email, wording, None, now, about=alerts.account(account.id))
    events.record(opened, now, "schedule_renewed", f"account {account.id}", f"the schedule now runs until {on_date(end, settings.timezone)}")
