from __future__ import annotations

TAKEOUT_URL = "https://takeout.google.com/"
TAKEOUT_MANAGE_URL = "https://takeout.google.com/manage"
TAKEOUT_SETTINGS_URL = "https://takeout.google.com/settings/takeout"

PAGE_BODY = "body"
SIGNED_IN_ACCOUNT_MENU = "a[aria-label*='Google Account']"
SIGNED_IN_ACCOUNT_LABEL_PREFIX = "google account:"
CREATE_NEW_EXPORT_HEADING = "text=/create a new export/i"
MANAGE_EXPORTS_HEADING = "text=/manage your exports/i"
EXPORT_LIST = "[role='list']"
DOWNLOAD_LINK = "a[href*='takeout.google.com/download']"

TYPEABLE: frozenset[str] = frozenset()

EXPORT_SELECT_NONE = "button:has-text('Deselect all')"
EXPORT_PHOTOS_CHECKBOX = "input[type='checkbox'][aria-label='Google Photos']"
EXPORT_NEXT_STEP = "button:has-text('Next step')"
EXPORT_DELIVERY_MENU = "[aria-label='Delivery method']"
EXPORT_DELIVERY_EMAIL = "[role='option']:has-text('Send download link via email')"
EXPORT_FREQUENCY_RECURRING = "input[type='radio'][aria-label*='Export every 2 months for 1 year']"
EXPORT_TYPE_MENU = "[aria-label='File type']"
EXPORT_TYPE_ZIP = "[role='option']:has-text('.zip')"
EXPORT_SIZE_MENU = "[aria-label='File size']"
EXPORT_SIZE_50GB = "[role='option']:has-text('50 GB')"
EXPORT_CREATE = "button:has-text('Create export')"
