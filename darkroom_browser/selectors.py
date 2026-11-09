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
EXPORT_PHOTOS_CHECKBOX = "input[type='checkbox'][name='Google Photos']"
EXPORT_NEXT_STEP = "button:has-text('Next step')"
EXPORT_DELIVERY_MENU = "[role='combobox'][aria-labelledby]"
EXPORT_DELIVERY_EMAIL = "[role='option'][data-value='TEMP']"
EXPORT_FREQUENCY_MONTHLY = "label:text-is('Export every month for 1 year')"
EXPORT_ONLY_NEW_AND_UPDATED = "label:text-is('Only new and updated items')"
EXPORT_TYPE_MENU = "[role='combobox'][aria-label='File type select']"
EXPORT_TYPE_ZIP = "[role='option'][data-value='ZIP']"
EXPORT_SIZE_MENU = "[role='combobox'][aria-label='File size select']"
EXPORT_SIZE_50GB = "[role='option'][data-value='53687091200']"
EXPORT_CREATE = "button:has-text('Create export')"
