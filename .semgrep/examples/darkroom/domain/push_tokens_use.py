from darkroom.domain import push_tokens
from darkroom.storage import secretbox


def read_the_token_here(device, secret):
    # ruleid: darkroom-push-token-one-module
    return secretbox.unseal(device.push_token_sealed, secret)


def clear_the_token_here(device):
    # ruleid: darkroom-push-token-one-module
    device.push_token_digest = None


def read_the_token_through_the_module(device, secret):
    # ok: darkroom-push-token-one-module
    return push_tokens.token_of(device, secret)
