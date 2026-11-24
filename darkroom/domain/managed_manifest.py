from __future__ import annotations

import json
from typing import Final, Literal

Resource = Literal["takeout_entry", "media_item"]
SCHEMA: Final = 2
RESOURCES: Final = ("takeout_entry", "media_item")
UNKNOWN: Final = "the import's resource manifest is of a schema Darkroom does not know, so nothing is sent or committed"


def of(resource: Resource) -> str:
    return json.dumps({"schema": SCHEMA, "resource": resource, "compound": "members"}, sort_keys=True)


TAKEOUT: Final = of("takeout_entry")
MEDIA_ITEM: Final = of("media_item")


def resource_of(raw: str) -> Resource | None:
    try:
        found = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if found == json.loads(TAKEOUT):
        return "takeout_entry"
    if found == json.loads(MEDIA_ITEM):
        return "media_item"
    return None


def known(raw: str) -> bool:
    return resource_of(raw) is not None
