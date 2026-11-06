def turn_cleanup_on_inside_eligibility(device):
    # ruleid: darkroom-cleanup-never-on
    device.cleanup_enabled = True


def mark_inside_eligibility(asset, now):
    # ok: darkroom-eligibility-single-path
    asset.eligible_at = now
