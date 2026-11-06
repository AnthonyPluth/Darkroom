from __future__ import annotations

import unittest
from typing import Annotated, get_args, get_origin, get_type_hints

from darkroom.server import contract, routes

REQUESTS = {
    "CheckRequest": contract.DeviceCheckBody, "AssetDescriptor": contract.DeviceAssetDecl, "ResourceDescriptor": contract.DeviceResourceDecl,
    "CheckinRequest": contract.DeviceCheckinBody,
}
REPLIES = {
    "CheckReply": contract.DeviceCheckReply, "NeededResource": contract.NeededResource, "DeviceMeReply": contract.DeviceMeReply,
}
ROUTES = {
    ("POST", "/api/device/assets/check"): ("CheckRequest", "CheckReply"),
    ("POST", "/api/device/checkin"): ("CheckinRequest", None),
    ("GET", "/api/device/me"): (None, "DeviceMeReply"),
}


class DeviceRoutesAreTyped(unittest.TestCase):
    def test_the_device_routes_are_annotated_with_these_types(self):
        handlers = {(route.method, route.path): route.handler for route in routes.ROUTES}
        for (method, path), (request, reply) in ROUTES.items():
            with self.subTest(route=path):
                hints = get_type_hints(handlers[(method, path)], include_extras=True)
                if request is not None:
                    self.assertIs(get_args(hints["request"])[1], REQUESTS[request])
                    self.assertIs(get_origin(hints["request"]), Annotated)
                if reply is not None:
                    self.assertIs(get_args(hints["return"])[1], REPLIES[reply])
