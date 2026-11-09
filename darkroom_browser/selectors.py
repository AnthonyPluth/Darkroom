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
EXPORT_ID_ATTRIBUTE = "data-export-id"
EXPORT_TIME_ATTRIBUTE = "data-time"
EXPORT_CREATED_TIME = "created"
EXPORT_EXPIRES_TIME = "expires"
DOWNLOAD_HOST = "takeout.google.com"
PART_LINK_TEMPLATE = 'a[href="{href}"]'

TYPEABLE: frozenset[str] = frozenset()

EXPORT_SELECT_NONE = "button:has-text('Deselect all')"
EXPORT_PHOTOS_CHECKBOX = ":is(input[type='checkbox'], [role='checkbox']):is([aria-label='Google Photos' i], [aria-label$=' Google Photos' i])"
EXPORT_NEXT_STEP = "button:has-text('Next step')"
EXPORT_DELIVERY_MENU = "[aria-label='Delivery method']"
EXPORT_DELIVERY_EMAIL = "[role='option']:has-text('Send download link via email')"
EXPORT_FREQUENCY_MONTHLY = ":is(input[type='radio'], [role='radio']):is([aria-label*='every month' i], [aria-label*='every 1 month' i], [aria-label*='monthly' i])"
EXPORT_TYPE_MENU = "[aria-label='File type']"
EXPORT_TYPE_ZIP = "[role='option']:has-text('.zip')"
EXPORT_SIZE_MENU = "[aria-label='File size']"
EXPORT_SIZE_50GB = "[role='option']:has-text('50 GB')"
EXPORT_CREATE = "button:has-text('Create export')"
