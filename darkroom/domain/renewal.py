from __future__ import annotations

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


def ending_key(account_id: int) -> str:
    return f"schedule:ending:{account_id}"


def renewed_key(account_id: int, end: float) -> str:
    return f"schedule:renewed:{account_id}:{int(end)}"


def on_date(moment: float, zone: str) -> str:
    local = datetime.fromtimestamp(moment, ZoneInfo(zone))
    return f"{local.day} {local:%b %Y}"


def renewal_asked(opened: Session, account: GoogleAccount, since: float) -> bool:
    found = opened.execute(
        select(BrowserJob.id).where(BrowserJob.account_id == account.id, BrowserJob.kind == browser_jobs.RENEW_KIND, BrowserJob.created >= since).limit(1)
    ).first()
    return found is not None


def ending_link(settings: Settings) -> str | None:
    return f"{settings.public_url}/exports" if settings.public_url else None


def watch_schedules(opened: Session, settings: Settings, now: float) -> int:
    queued = 0
    ending = select(GoogleAccount).where(GoogleAccount.schedule_end.is_not(None)).order_by(GoogleAccount.id)
    for account in opened.execute(ending).scalars().all():
        end = account.schedule_end
        assert end is not None
        if now < end - NOTICE:
            continue
        wording = alerts.schedule_ending(account.email, on_date(end, settings.timezone))
        alerts.open_alert(opened, settings, ending_key(account.id), ENDING, account.email, wording, ending_link(settings), now)
        if renewal_asked(opened, account, end - NOTICE):
            continue
        browser_jobs.queue(opened, account.id, browser_jobs.RENEW_KIND, {"email": account.email, "ending": end}, now)
        events.record(opened, now, "schedule_renewal_queued", f"account {account.id}", f"the schedule ends {on_date(end, settings.timezone)}")
        queued += 1
    return queued


def renewed(opened: Session, settings: Settings, job: BrowserJob, now: float) -> None:
    account = browser_jobs.account_of(opened, job.account_id)
    end = account.schedule_end
    assert end is not None
    alerts.close(opened, ending_key(account.id), now)
    wording = alerts.schedule_renewed(account.email, on_date(end, settings.timezone))
    alerts.open_alert(opened, settings, renewed_key(account.id, end), RENEWED, account.email, wording, None, now)
    events.record(opened, now, "schedule_renewed", f"account {account.id}", f"the schedule now runs until {on_date(end, settings.timezone)}")
