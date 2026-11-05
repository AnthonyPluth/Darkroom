import json
import time
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.domain import devices, push_tokens, wake
from darkroom.providers import apns
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Device, Event, User

from tests.privacy import no_leaks
from tests.test_apns import BUNDLE_ID, KEY_ID, TEAM_ID, ApnsWorld
from tests.test_push import JO_TOKEN, SAM_TOKEN, SECRET, FakePusher, PushWorld

DAY = 24 * 3600.0


class WakeWorld(PushWorld):
    def check_in_at(self, device_id, when):
        with db.session(self.engine) as opened:
            opened.get(Device, device_id).last_checkin = when

    def quiet(self, device_id=None, days=2.0):
        self.check_in_at(device_id or self.device_id, self.now - days * DAY)

    def wake(self, pusher, at=None):
        with db.session(self.engine) as opened:
            return wake.wake_due(opened, self.settings, pusher, at if at is not None else self.now)

    def wake_state(self, device_id):
        with db.session(self.engine) as opened:
            return opened.get(Device, device_id).wake_pushed_at

    def wake_events(self):
        return [(e.type, e.subject) for e in self.event_rows() if e.type.startswith("wake_")]

    def event_rows(self):
        with db.session(self.engine) as opened:
            rows = list(opened.execute(select(Event).order_by(Event.id)).scalars())
            opened.expunge_all()
            return rows

    def between_read_and_send(self, change):
        real = push_tokens.still_held

        def wrapped(*args, **kwargs):
            with db.session(self.engine) as other:
                change(other)
            return real(*args, **kwargs)

        return mock.patch.object(push_tokens, "still_held", wrapped)


class WhenToWakeTests(WakeWorld):
    def test_a_phone_quiet_for_over_a_day_gets_one_silent_wake_with_no_content(self):
        self.quiet()
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 1)
        self.assertEqual(pusher.sent, [([JO_TOKEN], apns.Wake())])
        self.assertEqual(self.wake_events(), [("wake_sent", "jo")])
        self.assertEqual(self.wake_state(self.device_id), self.now)
        self.assertIsNone(self.wake_state(self.sam_device))

    def test_only_quiet_phones_are_woken(self):
        self.quiet()
        self.check_in_at(self.sam_device, self.now - DAY + 60)
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 1)
        self.assertEqual([tokens for tokens, _ in pusher.sent], [[JO_TOKEN]])

    def test_a_check_in_inside_the_day_is_never_woken_and_exactly_a_day_is(self):
        self.check_in_at(self.device_id, self.now - DAY + 1)
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])
        self.check_in_at(self.device_id, self.now - DAY)
        self.assertEqual(self.wake(pusher), 1)

    def test_a_phone_that_never_checked_in_is_not_woken(self):
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_one_wake_per_interval_then_another_when_still_quiet(self):
        self.quiet()
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 1)
        for later in (60, 3600, DAY - 1):
            self.assertEqual(self.wake(pusher, self.now + later), 0)
        self.assertEqual(len(pusher.sent), 1)
        self.assertEqual(self.wake(pusher, self.now + DAY), 1)
        self.assertEqual(len(pusher.sent), 2)
        self.assertEqual(self.wake_state(self.device_id), self.now + DAY)

    def test_a_check_in_after_a_wake_ends_the_waking_until_it_is_quiet_again(self):
        self.quiet()
        pusher = FakePusher()
        self.wake(pusher)
        self.check_in_at(self.device_id, self.now + DAY)
        self.assertEqual(self.wake(pusher, self.now + 2 * DAY - 1), 0)
        self.assertEqual(self.wake(pusher, self.now + 2 * DAY), 1)

    def test_a_real_check_in_through_the_api_resets_the_quiet_time(self):
        self.quiet()
        status, _ = self.call("POST", "/api/device/checkin", {"pending_uploads": 0, "app_version": "1.0"})
        self.assertEqual(status, 200)
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher, time.time()), 0)

    def test_without_apns_nothing_is_woken_or_recorded(self):
        self.quiet()
        self.assertEqual(self.wake(None), 0)
        self.assertIsNone(self.wake_state(self.device_id))
        self.assertEqual(self.wake_events(), [])


class WhoIsNeverWokenTests(WakeWorld):
    def test_a_revoked_phone_is_not_woken(self):
        self.quiet()
        with db.session(self.engine) as opened:
            devices.revoke(opened, self.device_id, self.now)
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_a_phone_without_a_push_token_is_not_woken(self):
        self.quiet()
        with db.session(self.engine) as opened:
            push_tokens.forget_own(opened, self.device_id, self.now)
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])
        self.assertIsNone(self.wake_state(self.device_id))
        self.register(self.device_id, JO_TOKEN)
        self.assertEqual(self.wake(pusher), 1)

    def test_a_phone_whose_owner_can_no_longer_sign_in_is_not_woken(self):
        self.quiet()
        with db.session(self.engine) as opened:
            opened.get(User, "sub-jo").last_seen = 1.0
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_a_token_that_cannot_be_unsealed_is_not_woken(self):
        self.quiet()
        with db.session(self.engine) as opened:
            opened.get(Device, self.device_id).push_token_sealed = "enc:v1:garbage"
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_a_phone_revoked_after_the_candidates_were_read_is_not_woken(self):
        self.quiet()
        pusher = FakePusher()
        with self.between_read_and_send(lambda other: devices.revoke(other, self.device_id, self.now)):
            self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])
        self.assertEqual(self.wake_events(), [])

    def test_an_owner_whose_access_lapses_just_before_sending_is_not_woken(self):
        self.quiet()
        pusher = FakePusher()
        with self.between_read_and_send(lambda other: setattr(other.get(User, "sub-jo"), "last_seen", 1.0)):
            self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_a_phone_that_swaps_its_token_just_before_sending_is_not_sent_the_old_one(self):
        self.quiet()
        pusher = FakePusher()
        other_token = "6c" * 32
        with self.between_read_and_send(lambda other: push_tokens.register(other, self.settings, self.device_id, other_token, self.now)):
            self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_a_phone_that_checks_in_just_before_sending_is_not_woken(self):
        self.quiet()
        pusher = FakePusher()
        with self.between_read_and_send(lambda other: setattr(other.get(Device, self.device_id), "last_checkin", self.now)):
            self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])

    def test_two_runs_at_once_wake_a_phone_only_once(self):
        self.quiet()
        with db.session(self.engine) as first, db.session(self.engine) as second:
            self.assertTrue(wake.claim(first, self.device_id, self.now))
            self.assertFalse(wake.claim(second, self.device_id, self.now))
        pusher = FakePusher()
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(pusher.sent, [])


class WhenApnsAnswersTests(WakeWorld):
    def test_a_failed_wake_is_recorded_and_not_repeated_before_the_interval(self):
        self.quiet()
        pusher = FakePusher({JO_TOKEN: apns.Result("failed", "Unreachable")})
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(self.wake(pusher, self.now + 600), 0)
        self.assertEqual(len(pusher.sent), 1)
        self.assertEqual(self.wake_events(), [("wake_failed", "jo")])
        self.assertEqual(self.wake_state(self.device_id), self.now)

    def test_a_token_apns_reports_dead_is_forgotten_and_the_phone_is_not_woken_again(self):
        self.quiet()
        pusher = FakePusher({JO_TOKEN: apns.Result("invalid_token", "Unregistered", self.now + 10)})
        self.assertEqual(self.wake(pusher), 0)
        self.assertEqual(self.wake_events(), [("wake_token_invalid", "jo")])
        self.assertIsNone(self.token(self.device_id))
        self.assertEqual(self.wake(pusher, self.now + 2 * DAY), 0)
        self.assertEqual(len(pusher.sent), 1)

    def test_one_failing_phone_does_not_stop_the_next(self):
        self.quiet()
        self.quiet(self.sam_device)

        class Exploding(FakePusher):
            def send(self, tokens, push_):
                if tokens == [JO_TOKEN]:
                    raise RuntimeError("boom")
                return super().send(tokens, push_)

        pusher = Exploding()
        self.assertEqual(self.wake(pusher), 1)
        self.assertEqual(pusher.sent[0][0], [SAM_TOKEN])

    def test_the_wake_leaves_no_token_in_the_log_events_or_database_copy_beyond_the_sealed_one(self):
        self.quiet()
        with no_leaks(self, JO_TOKEN, SAM_TOKEN, SECRET, database=self.engine):
            self.assertEqual(self.wake(FakePusher()), 1)
        self.assertNotIn(JO_TOKEN, json.dumps([(e.subject, e.detail) for e in self.event_rows()]))


class SchedulerTests(WakeWorld):
    def test_the_push_job_wakes_quiet_phones(self):
        self.quiet()
        pusher = FakePusher()
        scheduler = jobs.build(self.settings, self.engine, self.now, pusher=pusher)
        scheduler.run_one("alerts.push", self.now)
        self.assertEqual(pusher.sent, [([JO_TOKEN], apns.Wake())])

    def test_alert_pushes_and_wakes_do_not_mix(self):
        from darkroom.domain import alerts

        self.quiet()
        self.open(alerts.person(self.person_id))
        pusher = FakePusher()
        jobs.push_alerts(self.settings, self.engine, pusher, self.now)
        kinds = [type(push_).__name__ for _, push_ in pusher.sent]
        self.assertEqual(kinds, ["Push", "Wake"])


class WakeOnTheWireTests(ApnsWorld):
    def test_a_wake_is_a_background_push_with_nothing_but_content_available(self):
        fake = self.start()
        results = self.client().send(["a1" * 32], apns.Wake())
        self.assertEqual(results, [apns.Result("sent", "")])
        received = fake.state.received[0]
        self.assertEqual(json.loads(received.body), {"aps": {"content-available": 1}})
        self.assertEqual(received.headers["apns-push-type"], "background")
        self.assertEqual(received.headers["apns-priority"], "5")
        self.assertEqual(received.headers["apns-topic"], BUNDLE_ID)
        self.assertEqual(received.headers["apns-collapse-id"], "wake")
        self.assertEqual(int(received.headers["apns-expiration"]), int(self.clock.now) + apns.WAKE_EXPIRES_AFTER)
        self.assertEqual(received.claims, {"iss": TEAM_ID, "iat": int(self.clock.now)})

    def test_a_wake_payload_carries_no_alert_sound_title_kind_id_or_name(self):
        text = apns.payload_of(apns.Wake()).decode()
        for forbidden in ("alert", "sound", "title", "kind", "alert_id", "badge", "thread"):
            self.assertNotIn(forbidden, text)

    def test_an_alert_push_is_still_an_alert_with_priority_10(self):
        fake = self.start()
        self.client().send(["a1" * 32], apns.Push("failure", 7, "Darkroom"))
        self.assertEqual(fake.state.received[0].headers["apns-push-type"], "alert")
        self.assertEqual(fake.state.received[0].headers["apns-priority"], "10")

    def test_pushes_go_only_to_apples_production_host(self):
        self.assertEqual(apns.PRODUCTION_HOST, "api.push.apple.com")
        source = Path(apns.__file__).read_text()
        self.assertNotIn("sandbox", source)
        self.assertNotIn("development.push", source)
        credentials = apns.Credentials(KEY_ID, TEAM_ID, BUNDLE_ID, self.key_file)
        client = apns.Apns(credentials, self.clock)
        self.assertEqual((client.host, client.port), ("api.push.apple.com", 443))

    def test_a_wake_to_a_bad_token_is_never_sent(self):
        fake = self.start()
        self.assertEqual(self.client().send(["not hex"], apns.Wake()), [apns.Result("invalid_token", "BadDeviceToken")])
        self.assertEqual(fake.state.received, [])


if __name__ == "__main__":
    unittest.main()
