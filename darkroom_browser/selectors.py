from __future__ import annotations

TAKEOUT_URL = "https://takeout.google.com/"
TAKEOUT_MANAGE_URL = "https://takeout.google.com/manage"
TAKEOUT_SETTINGS_URL = "https://takeout.google.com/settings/takeout"

PAGE_BODY = "body"
SIGNED_IN_ACCOUNT_MENU = "a[aria-label*='Google Account']"
CREATE_NEW_EXPORT_HEADING = "text=/create a new export/i"
MANAGE_EXPORTS_HEADING = "text=/manage your exports/i"
EXPORT_LIST = "[role='list']"
DOWNLOAD_LINK = "a[href*='takeout.google.com/download']"

TYPEABLE: frozenset[str] = frozenset()
