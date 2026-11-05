import contextlib
import io
import time
import unittest
from dataclasses import replace
from unittest import mock

from sqlalchemy import select

from darkroom.domain import devices, push_tokens
from darkroom.server.api import device as device_api
from darkroom.storage import db, secretbox
from darkroom.storage.models import Device, Event, User
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

    def token(self, device=None):
        device = device or self.device()
        with db.session(self.engine) as opened:
            return push_tokens.token_for(opened, self.settings, device, time.time())

    def kinds(self):
        with db.session(self.engine) as opened:
            return [event.type for event in opened.execute(select(Event).order_by(Event.id)).scalars() if event.type.startswith("push_token")]

    def test_a_device_registers_its_token_sealed_and_never_in_plain_text(self):
        with no_leaks(self, PUSH_TOKEN, TOKEN, SECRET, database=self.engine, replies=self.replies):
            self.assertEqual(self.register(f"  {PUSH_TOKEN.upper()} "), (200, {"ok": True}))
        device = self.device()
        self.assertTrue(secretbox.is_sealed(device.push_token_sealed or ""))
        self.assertEqual(self.token(device), PUSH_TOKEN)
        self.assertIsNotNone(device.push_token_at)
        self.assertEqual(self.kinds(), ["push_token_registered"])

    def test_registering_the_same_token_again_only_refreshes_when_it_was_seen(self):
        self.register()
        first = self.device().push_token_at
        self.assertEqual(self.register()[0], 200)
        self.assertGreaterEqual(self.device().push_token_at or 0, first or 0)
        self.assertEqual(self.kinds(), ["push_token_registered"])
        self.assertEqual(self.register(OTHER_PUSH_TOKEN)[0], 200)
        self.assertEqual(self.token(), OTHER_PUSH_TOKEN)
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_registered"])

    def test_a_token_moves_to_the_same_persons_device_that_registers_it_last(self):
        _, other_id = self.enrol("jo", "jo@example.test", OTHER_TOKEN)
        self.register()
        self.assertEqual(self.register(device_token=OTHER_TOKEN)[0], 200)
        self.assertIsNone(self.token())
        self.assertIsNone(self.device().push_token_digest)
        self.assertEqual(self.token(self.device(other_id)), PUSH_TOKEN)
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_cleared", "push_token_registered"])

    def test_another_persons_signed_in_device_cannot_take_the_token(self):
        _, other_id = self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.register()
        status, reply = self.register(device_token=OTHER_TOKEN)
        self.assertEqual(status, 409)
        self.assertNotIn(PUSH_TOKEN, str(reply))
        self.assertEqual(self.token(), PUSH_TOKEN)
        self.assertIsNone(self.device(other_id).push_token_digest)
        self.assertEqual(self.kinds(), ["push_token_registered"])

    def test_another_person_can_take_the_token_once_its_holder_cannot_sign_in(self):
        _, other_id = self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.register()
        with db.session(self.engine) as opened:
            opened.get(User, "sub-jo").email_verified = False
        self.assertEqual(self.register(device_token=OTHER_TOKEN)[0], 200)
        self.assertIsNone(self.device().push_token_digest)
        self.assertEqual(self.token(self.device(other_id)), PUSH_TOKEN)

    def test_another_persons_signed_in_device_keeps_the_token_even_when_it_cannot_be_read(self):
        _, other_id = self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.register()
        for damage in ("enc:v1:damaged", None):
            with self.subTest(damage=damage):
                with db.session(self.engine) as opened:
                    opened.get(Device, self.device_id).push_token_sealed = damage
                self.assertEqual(self.register(device_token=OTHER_TOKEN)[0], 409)
                self.assertEqual(self.device().push_token_digest, push_tokens.digest_of(PUSH_TOKEN))
                self.assertIsNone(self.device(other_id).push_token_digest)

    def test_the_same_token_is_sealed_again_when_the_stored_one_cannot_be_read(self):
        self.register()
        with db.session(self.engine) as opened:
            opened.get(Device, self.device_id).push_token_sealed = secretbox.seal(PUSH_TOKEN, "the-old-secret-key")
        self.assertIsNone(self.token())
        self.assertEqual(self.register(), (200, {"ok": True}))
        self.assertEqual(self.token(), PUSH_TOKEN)

    def test_registering_the_same_token_after_the_key_was_cleared_is_refused(self):
        self.register()
        with db.session(self.engine) as opened:
            with self.assertRaises(secretbox.Unreadable):
                push_tokens.register(opened, replace(self.settings, secret_key=""), self.device_id, PUSH_TOKEN, 9.0)
        with db.session(self.engine) as opened:
            self.assertNotEqual(opened.get(Device, self.device_id).push_token_at, 9.0)

    def test_two_devices_registering_the_same_token_at_once_do_not_fail(self):
        _, other_id = self.enrol("jo", "jo@example.test", OTHER_TOKEN)
        self.register()
        real, calls = push_tokens.holders, []

        def raced(opened, digest, device):
            calls.append(device.id)
            return [] if len(calls) == 1 else real(opened, digest, device)

        with mock.patch.object(push_tokens, "holders", raced):
            self.assertEqual(self.register(device_token=OTHER_TOKEN), (200, {"ok": True}))
        self.assertEqual(calls, [other_id, other_id])
        self.assertIsNone(self.device().push_token_digest)
        self.assertEqual(self.token(self.device(other_id)), PUSH_TOKEN)
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
            push_tokens.register(opened, self.settings, self.device_id, PUSH_TOKEN, 6.0)
        with db.session(self.engine) as opened, self.assertRaises(LookupError):
            push_tokens.register(opened, self.settings, 999, PUSH_TOKEN, 6.0)

    def test_a_device_revoked_between_sign_in_and_the_request_gets_401(self):
        _, other_id = self.enrol("jo", "jo@example.test", OTHER_TOKEN)
        self.register()
        real = device_api.device_of

        def revoked_right_after(request):
            who = real(request)
            with db.session(self.engine) as opened:
                devices.revoke(opened, who.device_id, 5.0)
            return who

        for method, body, device_token, device_id in (("PUT", {"token": OTHER_PUSH_TOKEN}, TOKEN, self.device_id), ("DELETE", None, OTHER_TOKEN, other_id)):
            with self.subTest(method=method), mock.patch.object(device_api, "device_of", revoked_right_after), contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(self.call(method, "/api/device/push-token", body, device_token)[0], 401)
                self.assertIsNotNone(self.device(device_id).revoked_at)
        self.assertIsNone(self.device().push_token_digest)
        self.assertEqual(self.kinds(), ["push_token_registered", "push_token_cleared"])

    def test_a_token_is_not_read_with_another_key_tampered_with_or_from_a_revoked_device(self):
        self.register()
        device = self.device()
        with db.session(self.engine) as opened:
            self.assertIsNone(push_tokens.token_for(opened, replace(self.settings, secret_key="another-secret-key"), device, time.time()))
        device.push_token_digest = push_tokens.digest_of(OTHER_PUSH_TOKEN)
        self.assertIsNone(self.token(device))
        device = self.device()
        device.revoked_at = 1.0
        self.assertIsNone(self.token(device))

    def test_a_device_whose_owner_can_still_sign_in_has_its_token(self):
        self.register()
        self.assertEqual(self.token(), PUSH_TOKEN)

    def lapse(self, change):
        self.register()
        with db.session(self.engine) as opened:
            change(opened)
        self.assertIsNone(self.token())
        self.assertIsNotNone(self.device().push_token_sealed)

    def test_no_token_once_the_owner_was_removed(self):
        def removed(opened):
            opened.get(User, "sub-jo").groups = "[]"
        self.lapse(removed)

    def test_no_token_once_the_owners_sign_in_lapsed(self):
        def lapsed(opened):
            opened.get(User, "sub-jo").last_seen = time.time() - 31 * 86_400
        self.lapse(lapsed)

    def test_no_token_once_the_owners_email_moved_to_another_account(self):
        def moved(opened):
            opened.add(User(sub="sub-jo-again", email="jo@example.test", email_verified=True, groups='["family"]', last_seen=time.time() + 5))
        self.lapse(moved)

    def test_no_token_once_sign_in_is_not_set_up(self):
        self.register()
        with db.session(self.engine) as opened:
            self.assertIsNone(push_tokens.token_for(opened, replace(self.settings, oidc_issuer=""), self.device(), time.time()))


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
