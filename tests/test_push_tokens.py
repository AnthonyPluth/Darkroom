import contextlib
import io
import unittest

from sqlalchemy import select

from darkroom.domain import devices, push_tokens
from darkroom.storage import db, secretbox
from darkroom.storage.models import Device, Event
from tests.device_world import OTHER_TOKEN, TOKEN, DeviceCase
from tests.privacy import no_leaks

SECRET = "push-token-test-secret-key"
PUSH_TOKEN = "9f" * 32
OTHER_PUSH_TOKEN = "8e" * 40


class PushTokenTests(DeviceCase):
    extra_env = {"DARKROOM_SECRET_KEY": SECRET}

    def register(self, token=PUSH_TOKEN, device_token=TOKEN):
        with contextlib.redirect_stderr(io.StringIO()):
            return self.call("PUT", "/api/device/push-token", {"token": token}, device_token)

    def device(self, device_id=None):
        with db.session(self.engine) as opened:
            device = opened.get(Device, device_id or self.device_id)
            opened.expunge(device)
            return device

    def kinds(self):
        with db.session(self.engine) as opened:
            return [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars() if event.type.startswith("push_token")]

    def test_a_device_registers_its_token_sealed_and_never_in_plain_text(self):
        with no_leaks(self, PUSH_TOKEN, TOKEN, SECRET, database=self.engine, replies=self.replies):
            self.assertEqual(self.register(f"  {PUSH_TOKEN.upper()} "), (200, {"ok": True}))
        device = self.device()
        self.assertTrue(secretbox.is_sealed(device.push_token_sealed or ""))
        self.assertEqual(push_tokens.token_of(device, SECRET), PUSH_TOKEN)
        self.assertIsNotNone(device.push_token_at)
        self.assertEqual(self.kinds(), ["push_token_registered"])

    def test_registering_the_same_token_again_only_refreshes_when_it_was_seen(self):
        self.register()
        first = self.device().push_token_at
        self.assertEqual(self.register()[0], 200)
        self.assertGreaterEqual(self.device().push_token_at or 0, first or 0)
        self.assertEqual(self.kinds(), ["push_token_registered"])
        self.assertEqual(self.register(OTHER_PUSH_TOKEN)[0], 200)
        self.assertEqual(push_tokens.token_of(self.device(), SECRET), OTHER_PUSH_TOKEN)
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_registered"])

    def test_a_token_moves_to_the_device_that_registers_it_last(self):
        _, other_id = self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.register()
        self.assertEqual(self.register(device_token=OTHER_TOKEN)[0], 200)
        self.assertIsNone(push_tokens.token_of(self.device(), SECRET))
        self.assertIsNone(self.device().push_token_digest)
        self.assertEqual(push_tokens.token_of(self.device(other_id), SECRET), PUSH_TOKEN)
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_cleared", "push_token_registered"])

    def test_bad_tokens_are_400_and_store_nothing(self):
        for body in ({}, {"token": ""}, {"token": 5}, {"token": "ab" * 31}, {"token": "ab" * 101}, {"token": "zz" * 32}, {"token": "9f" * 32 + " x"},
                     {"token": " " * 200 + "9f" * 32}):
            with self.subTest(body=body), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.call("PUT", "/api/device/push-token", body)[0], 400)
        self.assertIsNone(self.device().push_token_sealed)
        self.assertEqual(self.kinds(), [])

    def test_the_app_can_clear_its_token(self):
        self.register()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(self.call("DELETE", "/api/device/push-token"), (200, {"ok": True}))
            self.assertEqual(self.call("DELETE", "/api/device/push-token"), (200, {"ok": True}))
        device = self.device()
        self.assertEqual((device.push_token_sealed, device.push_token_digest), (None, None))
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_cleared"])

    def test_revoking_a_device_forgets_its_token_and_it_cannot_register_again(self):
        self.register()
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, 5.0)
        device = self.device()
        self.assertEqual((device.push_token_sealed, device.push_token_digest), (None, None))
        self.assertEqual(self.register()[0], 401)
        self.assertEqual(self.call("DELETE", "/api/device/push-token")[0], 401)
        self.assertEqual(self.register(device_token=None)[0], 401)
        with db.session(self.engine) as opened, self.assertRaises(LookupError):
            push_tokens.register(opened, SECRET, self.device_id, PUSH_TOKEN, 6.0)
        with db.session(self.engine) as opened, self.assertRaises(LookupError):
            push_tokens.register(opened, SECRET, 999, PUSH_TOKEN, 6.0)

    def test_a_token_is_not_read_with_another_key_tampered_with_or_from_a_revoked_device(self):
        self.register()
        device = self.device()
        self.assertIsNone(push_tokens.token_of(device, "another-secret-key"))
        device.push_token_digest = push_tokens.digest_of(OTHER_PUSH_TOKEN)
        self.assertIsNone(push_tokens.token_of(device, SECRET))
        device = self.device()
        device.revoked_at = 1.0
        self.assertIsNone(push_tokens.token_of(device, SECRET))


class NoSecretKeyTests(DeviceCase):
    def test_without_a_secret_key_the_token_is_refused_and_nothing_stored(self):
        with contextlib.redirect_stderr(io.StringIO()):
            status, reply = self.call("PUT", "/api/device/push-token", {"token": PUSH_TOKEN})
        self.assertEqual(status, 503)
        self.assertIn("DARKROOM_SECRET_KEY", reply["error"])
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Device, self.device_id).push_token_sealed)


if __name__ == "__main__":
    unittest.main()
