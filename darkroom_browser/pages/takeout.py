from __future__ import annotations

import html
import re
from dataclasses import dataclass

SIGNED_IN = "SIGNED_IN"
SIGN_IN_NEEDED = "SIGN_IN_NEEDED"
VERIFY_ITS_YOU = "VERIFY_ITS_YOU"
UNKNOWN = "UNKNOWN"

TAGS = re.compile(r"(?is)<(script|style)\b.*?</\1>|<[^>]+>")
SPACES = re.compile(r"\s+")
VERIFY_PHRASES = ("verify it's you", "verify its you", "confirm it's you", "confirm its you", "verify that it's you", "verify your identity",
                  "to help keep your account safe, google wants to make sure it's really you", "this device isn't recognized", "unusual activity")
SIGN_IN_PHRASES = ("sign in to continue", "use your google account", "sign in with your google account", "choose an account", "to continue to google takeout")
SIGN_IN_FORMS = ("type=\"email\"", "type='email'", "identifierid", "accounts.google.com/signin", "accounts.google.com/v3/signin", "accounts.google.com/accountchooser")
SIGNED_IN_PHRASES = ("manage your exports", "create a new export", "your google data", "export progress", "select data to include", "download your data",
                     "exports available")


def visible_text(page: str) -> str:
    text = html.unescape(TAGS.sub(" ", page))
    return SPACES.sub(" ", text).lower().replace("’", "'")


def classify(page: str) -> str:
    text = visible_text(page)
    lowered = page.lower().replace("’", "'")
    if any(phrase in text for phrase in VERIFY_PHRASES):
        return VERIFY_ITS_YOU
    if any(phrase in text for phrase in SIGN_IN_PHRASES) or any(marker in lowered for marker in SIGN_IN_FORMS):
        return SIGN_IN_NEEDED
    if "takeout" in lowered and any(phrase in text for phrase in SIGNED_IN_PHRASES):
        return SIGNED_IN
    return UNKNOWN


CONFIRMATION_PHRASES = ("export created", "we've started creating a copy", "your export has been scheduled", "export scheduled")
RECURRING = re.compile(r"every 2 months for (?:1 year|a year|12 months)")


@dataclass(frozen=True)
class ScheduleConfirmation:
    frequency: str


def parse_schedule_confirmation(page: str) -> ScheduleConfirmation | None:
    text = visible_text(page)
    if not any(phrase in text for phrase in CONFIRMATION_PHRASES):
        return None
    recurring = RECURRING.search(text)
    return ScheduleConfirmation(recurring.group(0)) if recurring is not None else None
