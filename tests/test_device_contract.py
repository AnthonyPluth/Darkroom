from __future__ import annotations

import re
import typing
import unittest
from pathlib import Path
from typing import Annotated, get_args, get_origin, get_type_hints, is_typeddict

from sqlalchemy.orm import Session

from darkroom.server import contract, routes
from darkroom.storage.models import Device
from tests.device_world import DeviceCase
from tests.eligibility_world import Household

MODELS = Path(__file__).resolve().parent.parent / "ios" / "Packages" / "DarkroomCore" / "Sources" / "DarkroomCore" / "Models.swift"
SWIFT_SCALARS = {"String": str, "Int": int, "Int64": int, "Bool": bool, "Double": float}

REQUESTS = {
    "CheckRequest": contract.DeviceCheckBody, "AssetDescriptor": contract.DeviceAssetDecl, "ResourceDescriptor": contract.DeviceResourceDecl,
    "CheckinRequest": contract.DeviceCheckinBody, "EligibleRequest": contract.EligibleBody, "ConfirmRequest": contract.EligibleBody,
    "DeletedRequest": contract.DeletedBody,
}
REPLIES = {
    "CheckReply": contract.DeviceCheckReply, "NeededResource": contract.NeededResource, "EligibleReply": contract.EligibleReply,
    "EligibleAsset": contract.EligibleAsset, "ConfirmReply": contract.ConfirmReply, "RejectedAsset": contract.Refusal,
    "DeletedReply": contract.DeletedReply, "DeviceMeReply": contract.DeviceMeReply,
}
ROUTES = {
    ("POST", "/api/device/assets/check"): ("CheckRequest", "CheckReply"),
    ("POST", "/api/device/eligible"): ("EligibleRequest", "EligibleReply"),
    ("POST", "/api/device/eligible/confirm"): ("ConfirmRequest", "ConfirmReply"),
    ("POST", "/api/device/deleted"): ("DeletedRequest", "DeletedReply"),
    ("POST", "/api/device/checkin"): ("CheckinRequest", None),
    ("GET", "/api/device/me"): (None, "DeviceMeReply"),
}


def blocks(text: str) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for found in re.finditer(r"public (struct|enum) (\w+)[^{]*\{", text):
        depth, end = 1, found.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        out[found.group(2)] = (found.group(1), text[found.end():end - 1])
    return out


def swift_types() -> tuple[dict[str, dict[str, str]], set[str]]:
    structs: dict[str, dict[str, str]] = {}
    enums: set[str] = set()
    for name, (kind, body) in blocks(MODELS.read_text()).items():
        if kind == "enum":
            enums.add(name)
            continue
        fields = dict(re.findall(r"public let (\w+): ([\w\[\]?]+)", body))
        keys = {field: field for field in fields}
        coding = re.search(r"enum CodingKeys[^{]*\{(.*?)\}", body, re.S)
        if coding:
            for line in re.findall(r"case ([^\n]+)", coding.group(1)):
                for part in line.split(","):
                    named = re.match(r"\s*(\w+)(?:\s*=\s*\"([^\"]+)\")?", part)
                    if named:
                        keys[named.group(1)] = named.group(2) or named.group(1)
        structs[name] = {keys[field]: swift for field, swift in fields.items()}
    return structs, enums


def required_keys(typed: type) -> set[str]:
    return set(typing.cast(typing.Any, typed).__required_keys__)


def matches(swift: str, python: object, structs: dict[str, dict[str, str]], enums: set[str]) -> bool:
    if swift.endswith("?"):
        others = [arg for arg in get_args(python) if arg is not type(None)]
        return type(None) in get_args(python) and len(others) == 1 and matches(swift[:-1], others[0], structs, enums)
    if swift.startswith("["):
        return get_origin(python) is list and matches(swift[1:-1], get_args(python)[0], structs, enums)
    if swift in SWIFT_SCALARS:
        return python is SWIFT_SCALARS[swift]
    if swift in enums:
        return python is str
    return is_typeddict(python)


class SwiftModelsMatchTheServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.structs, cls.enums = swift_types()

    def test_the_parser_reads_the_models(self):
        self.assertEqual(self.structs["EligibleAsset"], {"asset_id": "String", "bytes": "Int64"})
        self.assertEqual(self.structs["DeletedRequest"], {"asset_ids": "[String]"})
        self.assertEqual(self.structs["EligibleReply"], {"cleanup_enabled": "Bool", "eligible": "[EligibleAsset]", "rejected": "[RejectedAsset]"})
        self.assertEqual(self.structs["RejectedAsset"], {"asset_id": "String", "reason": "String", "message": "String"})
        self.assertEqual(self.structs["DeletedReply"], {"recorded": "[String]", "rejected": "[RejectedAsset]"})
        self.assertIn("ResourceKind", self.enums)
        for name in (*REQUESTS, *REPLIES):
            self.assertIn(name, self.structs)

    def test_every_request_the_app_sends_carries_exactly_the_keys_the_server_reads(self):
        for name, typed in REQUESTS.items():
            with self.subTest(swift=name):
                hints = get_type_hints(typed)
                sent = self.structs[name]
                self.assertLessEqual(set(sent), set(hints))
                self.assertLessEqual(required_keys(typed), set(sent))
                for key, swift in sent.items():
                    accepted = [arg for arg in get_args(hints[key]) if arg is not type(None)] if not swift.endswith("?") else []
                    read = accepted[0] if len(accepted) == 1 and type(None) in get_args(hints[key]) else hints[key]
                    self.assertTrue(matches(swift, read, self.structs, self.enums), f"{name}.{key}: {swift} vs {hints[key]}")

    def test_every_reply_the_app_decodes_has_the_keys_and_types_it_needs(self):
        for name, typed in REPLIES.items():
            with self.subTest(swift=name):
                hints = get_type_hints(typed)
                self.assertLessEqual(set(self.structs[name]), required_keys(typed))
                for key, swift in self.structs[name].items():
                    self.assertTrue(matches(swift, hints[key], self.structs, self.enums), f"{name}.{key}: {swift} vs {hints[key]}")

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


class LiveRepliesDecodeAsTheAppExpects(DeviceCase):
    def decodes(self, name: str, value: object) -> None:
        structs, enums = swift_types()
        self.assertIsInstance(value, dict, name)
        assert isinstance(value, dict)
        for key, swift in structs[name].items():
            self.assertIn(key, value, f"{name} needs {key}")
            self.value_decodes(swift, value[key], structs, enums, f"{name}.{key}")

    def value_decodes(self, swift: str, value: object, structs, enums, where: str) -> None:
        if swift.endswith("?"):
            if value is not None:
                self.value_decodes(swift[:-1], value, structs, enums, where)
        elif swift.startswith("["):
            self.assertIsInstance(value, list, where)
            assert isinstance(value, list)
            for entry in value:
                self.value_decodes(swift[1:-1], entry, structs, enums, where)
        elif swift in SWIFT_SCALARS:
            self.assertIs(type(value), SWIFT_SCALARS[swift], where)
        elif swift in enums:
            self.assertIsInstance(value, str, where)
        else:
            self.decodes(swift, value)

    def test_the_who_am_i_reply_uses_the_apps_keys(self):
        status, reply = self.call("GET", "/api/device/me")
        self.assertEqual(status, 200)
        self.decodes("DeviceMeReply", reply)
        self.assertEqual(set(reply), set(swift_types()[0]["DeviceMeReply"]))

    def test_the_cleanup_round_trip_uses_the_apps_keys(self):
        opened = Session(self.engine, expire_on_commit=False)
        self.addCleanup(opened.close)
        home = Household(self, opened, self.root)
        jo = home.person("jo")
        phone = opened.get(Device, self.device_id)
        phone.cleanup_enabled = True
        google = home.account(jo)
        good, unmatched = home.photo(phone, through=google), home.photo(phone)
        opened.commit()
        assets = [{"id": entry.photokit_id, "resources": [{"kind": r.kind, "sha1": r.sha1, "bytes": r.bytes, "filename": r.filename} for r in entry.resources]}
                  for entry in (home.reported(good), home.reported(unmatched))]
        status, listed = self.call("POST", "/api/device/eligible", {"assets": assets})
        self.assertEqual(status, 200)
        self.decodes("EligibleReply", listed)
        self.assertEqual([entry["asset_id"] for entry in listed["eligible"]], [good.id])
        for entry in listed["rejected"]:
            self.decodes("RejectedAsset", entry)
        status, confirmed = self.call("POST", "/api/device/eligible/confirm", {"assets": assets})
        self.assertEqual(status, 200)
        self.decodes("ConfirmReply", confirmed)
        self.assertEqual((confirmed["confirmed"], [entry["asset_id"] for entry in confirmed["rejected"]]), ([good.id], [unmatched.id]))
        status, deleted = self.call("POST", "/api/device/deleted", {"asset_ids": [good.id]})
        self.assertEqual((status, deleted["recorded"]), (200, [good.id]))
        self.decodes("DeletedReply", deleted)
