def clear_inside_the_removed_module(device, now):
    # ruleid: darkroom-push-token-one-module
    device.push_token_at = now


def clear_every_column_when_revoking(device, now):
    # ruleid: darkroom-push-token-one-module
    device.push_token_sealed, device.push_token_digest, device.push_token_at = None, None, now


def clear_only_the_sealed_copy(device, now):
    # ruleid: darkroom-push-token-one-module
    device.push_token_sealed, device.push_token_digest = None, None


def store_a_token_under_the_cover_of_a_clear(device, sealed, now):
    # ruleid: darkroom-push-token-one-module
    device.push_token_sealed, device.push_token_digest, device.push_token_at = sealed, None, now
