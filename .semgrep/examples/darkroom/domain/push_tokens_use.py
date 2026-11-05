from darkroom.domain import push_tokens
from darkroom.storage import secretbox


def read_the_token_here(device, secret):
    # ruleid: darkroom-push-token-one-module
    return secretbox.unseal(device.push_token_sealed, secret)


def clear_the_token_here(device):
    # ruleid: darkroom-push-token-one-module
    device.push_token_digest = None


def touch_when_it_was_seen_here(device, now):
    # ruleid: darkroom-push-token-one-module
    device.push_token_at = now


def read_the_token_by_name(device, secret):
    # ruleid: darkroom-push-token-one-module
    return secretbox.unseal(getattr(device, "push_token_sealed"), secret)


def clear_the_token_by_name(device):
    # ruleid: darkroom-push-token-one-module
    setattr(device, "push_token_digest", None)


def read_another_column_by_name(device):
    # ok: darkroom-push-token-one-module
    return getattr(device, "name")


def read_the_token_through_the_module(opened, settings, device, now):
    # ok: darkroom-push-token-one-module
    return push_tokens.token_for(opened, settings, device, now)
