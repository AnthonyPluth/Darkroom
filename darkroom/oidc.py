from __future__ import annotations

from darkroom.config import Settings


def configured(settings: Settings) -> bool:
    return bool(settings.oidc_issuer and settings.oidc_client_id)
