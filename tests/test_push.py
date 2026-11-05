import contextlib
import io
import json
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

from sqlalchemy import select

from darkroom.domain import alerts, devices, push, push_tokens
from darkroom.providers import apns
from darkroom.server import jobs
from darkroom.storage import db
from darkroom.storage.models import Alert, Device, Event, GoogleAccount, Pixel, PixelPerson, User
from tests.device_world import OTHER_TOKEN, DeviceCase
from tests.fakes.apns import HOST, FakeApns, new_key
from tests.privacy import no_leaks
from tests.shared import settings_from
from tests.test_apns import BUNDLE_ID, KEY_ID, TEAM_ID, Clock, write_key

SECRET = "push-delivery-test-secret-key"
JO_TOKEN = "9f" * 32
SAM_TOKEN = "8e" * 40
MAX_TOKEN = "7d" * 32
THIRD_TOKEN = "device-token-canary-third-aa11bb22cc33dd44"


class FakePusher:
    def __init__(self, replies: dict[str, apns.Result] | None = None) -> None:
        self.replies = replies or {}
        self.sent: list[tuple[list[str], apns.Push]] = []

    def send(self, tokens, push_):
        self.sent.append((list(tokens), push_))
        return [self.replies.get(token, apns.Result("sent", "")) for token in tokens]


class PushWorld(DeviceCase):
    extra_env = {"DARKROOM_SECRET_KEY": SECRET}

    def setUp(self):
        super().setUp()
        self.now = time.time()
        self.sam_id, self.sam_device = self.enrol("sam", "sam@example.test", OTHER_TOKEN)
        self.register(self.device_id, JO_TOKEN)
        self.register(self.sam_device, SAM_TOKEN)

    def register(self, device_id, token, at=None):
        with db.session(self.engine) as opened:
            push_tokens.register(opened, self.settings, device_id, token, at if at is not None else self.now)

    def open(self, about, kind="sign_in_needed", key="k", subject="a@example.test", at=None):
        with db.session(self.engine) as opened:
            alert = alerts.open_alert(opened, self.settings, key, kind, subject, alerts.sign_in_needed(subject), None, at if at is not None else self.now,
                                      notifier=alerts.queued, about=about)
            return alert.id

    def deliver(self, pusher, at=None):
        with db.session(self.engine) as opened:
            return push.deliver_due(opened, self.settings, pusher, at if at is not None else self.now)

    def alert(self, alert_id):
        with db.session(self.engine) as opened:
            found = opened.get(Alert, alert_id)
            opened.expunge(found)
            return found

    def events(self, prefix=("alert_", "push_")):
        with db.session(self.engine) as opened:
            return [(e.type, e.subject) for e in opened.execute(select(Event).order_by(Event.id)).scalars()
                    if e.type.startswith(prefix) and e.type not in ("push_token_registered",)]

    def account(self):
        with db.session(self.engine) as opened:
            account = GoogleAccount(email="a@example.test", default_person_id=self.person_id, created=1.0)
            opened.add(account)
            opened.flush()
            pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
            opened.add(pixel)
            opened.flush()
            opened.add(PixelPerson(pixel_id=pixel.id, person_id=self.sam_id))
            return account.id

    def token(self, device_id):
        with db.session(self.engine) as opened:
            device = opened.get(Device, device_id)
            return push_tokens.token_for(opened, self.settings, device, self.now)


class DeliveryTests(PushWorld):
    def test_an_alert_about_a_person_is_pushed_only_to_that_persons_devices_once(self):
        alert_id = self.open(alerts.person(self.person_id), kind="originals_unmatched", subject="jo")
        pusher = FakePusher()
        self.assertEqual(self.deliver(pusher), 1)
        self.assertEqual(pusher.sent, [([JO_TOKEN], apns.Push("originals_unmatched", alert_id, alerts.TITLES["originals_unmatched"]))])
        self.assertIsNone(self.alert(alert_id).push_due)
        self.assertEqual(self.events(), [("alert_queued", "jo"), ("push_sent", "jo"), ("alert_sent", "jo")])
        self.assertEqual(self.deliver(pusher, self.now + 60), 0)
        self.assertEqual(len(pusher.sent), 1)

    def test_an_alert_about_an_account_goes_to_every_person_it_serves_and_nobody_else(self):
        _, max_device = self.enrol("max", "max@example.test", THIRD_TOKEN)
        self.register(max_device, MAX_TOKEN)
        with db.session(self.engine) as opened:
            account = GoogleAccount(email="a@example.test", default_person_id=self.person_id, created=1.0)
            opened.add(account)
            opened.flush()
            pixel = Pixel(name="Pixel", google_account_id=account.id, created=1.0)
            opened.add(pixel)
            opened.flush()
            opened.add(PixelPerson(pixel_id=pixel.id, person_id=self.sam_id))
            account_id = account.id
        self.open(alerts.account(account_id))
        pusher = FakePusher()
        self.assertEqual(self.deliver(pusher), 2)
        self.assertEqual(pusher.sent[0][0], [JO_TOKEN, SAM_TOKEN])

    def test_an_alert_concerning_nobody_or_only_people_without_a_usable_device_is_recorded_as_not_delivered(self):
        nobody = self.open(alerts.NOBODY, key="k1")
        with db.session(self.engine) as opened:
            opened.get(User, "sub-jo").last_seen = 1.0
            devices.revoke(opened, self.sam_device, self.now)
        lapsed = self.open(alerts.person(self.person_id), key="k2")
        revoked = self.open(alerts.person(self.sam_id), key="k3")
        pusher = FakePusher()
        self.assertEqual(self.deliver(pusher), 0)
        self.assertEqual(pusher.sent, [])
        self.assertEqual([t for t, _ in self.events() if t == "alert_no_device"], ["alert_no_device"] * 3)
        self.assertTrue(all(self.alert(each).push_due is None for each in (nobody, lapsed, revoked)))

    def test_without_apns_a_queued_push_is_recorded_as_not_sent(self):
        alert_id = self.open(alerts.person(self.person_id))
        self.assertEqual(self.deliver(None), 0)
        self.assertIsNone(self.alert(alert_id).push_due)
        self.assertEqual([t for t, _ in self.events()], ["alert_queued", "alert_disabled"])

    def test_a_repeating_alert_closed_before_its_push_is_not_pushed_but_a_one_shot_alert_is(self):
        repeating = self.open(alerts.person(self.person_id), key="k1")
        one_shot = self.open(alerts.person(self.person_id), kind="export_imported", key="k2", subject="export 1")
        with db.session(self.engine) as opened:
            alerts.close(opened, "k1", self.now)
        pusher = FakePusher()
        self.assertEqual(self.deliver(pusher), 1)
        self.assertEqual([sent.alert_id for _, sent in pusher.sent], [one_shot])
        self.assertIsNone(self.alert(repeating).push_due)

    def test_each_repeat_of_an_alert_is_pushed_again(self):
        self.open(alerts.person(self.person_id), at=self.now)
        pusher = FakePusher()
        self.deliver(pusher)
        with db.session(self.engine) as opened:
            self.assertEqual(alerts.tick(opened, self.settings, self.now + alerts.DAY, notifier=alerts.queued), 1)
        self.assertEqual(self.deliver(pusher, self.now + alerts.DAY), 1)
        self.assertEqual(len(pusher.sent), 2)


class InvalidTokenTests(PushWorld):
    def test_a_token_apns_reports_invalid_is_forgotten_and_the_other_device_still_gets_the_push(self):
        self.open(alerts.account(self.account()))
        pusher = FakePusher({JO_TOKEN: apns.Result("invalid_token", "Unregistered", self.now + 10)})
        self.assertEqual(self.deliver(pusher), 1)
        self.assertIsNone(self.token(self.device_id))
        self.assertEqual(self.token(self.sam_device), SAM_TOKEN)
        self.assertIn(("push_token_invalid", "jo"), self.events())
        self.assertIn(("push_token_cleared", "jo"), self.events(("push_token_cleared",)))

    def test_a_token_apns_calls_bad_without_a_time_is_forgotten(self):
        self.open(alerts.person(self.person_id))
        self.deliver(FakePusher({JO_TOKEN: apns.Result("invalid_token", "BadDeviceToken")}))
        self.assertIsNone(self.token(self.device_id))
        self.assertEqual([t for t, _ in self.events()][-2:], ["push_token_cleared", "alert_no_device"])

    def test_a_token_registered_again_after_apns_last_knew_it_valid_is_kept(self):
        self.open(alerts.person(self.person_id))
        self.register(self.device_id, JO_TOKEN, at=self.now + 5)
        pusher = FakePusher({JO_TOKEN: apns.Result("invalid_token", "Unregistered", self.now - 10)})
        self.deliver(pusher)
        self.assertEqual(self.token(self.device_id), JO_TOKEN)
        self.assertEqual(self.events(("push_token_cleared",)), [])

    def test_a_token_replaced_while_the_push_was_out_is_not_forgotten(self):
        self.open(alerts.person(self.person_id))
        world = self

        class ReplacingPusher(FakePusher):
            def send(self, tokens, push_):
                world.register(world.device_id, MAX_TOKEN, at=world.now + 1)
                return [apns.Result("invalid_token", "BadDeviceToken") for _ in tokens]

        self.deliver(ReplacingPusher())
        self.assertEqual(self.token(self.device_id), MAX_TOKEN)



class RetryTests(PushWorld):
    def test_an_unreachable_apns_is_retried_for_an_hour_then_recorded_as_failed(self):
        alert_id = self.open(alerts.person(self.person_id))
        pusher = FakePusher({JO_TOKEN: apns.Result("failed", "Unreachable")})
        at = self.now
        tries = 0
        while self.alert(alert_id).push_due is not None:
            self.assertEqual(self.deliver(pusher, at), 0)
            tries += 1
            at += push.RETRY_AFTER
        self.assertEqual(tries, int(push.RETRY_FOR // push.RETRY_AFTER) + 1)
        self.assertEqual(self.deliver(pusher, at + push.RETRY_FOR), 0)
        kinds = [t for t, _ in self.events()]
        self.assertEqual(kinds.count("push_failed"), tries)
        self.assertEqual(kinds[-1], "alert_failed")

    def test_a_push_apns_refuses_for_another_reason_is_not_retried(self):
        alert_id = self.open(alerts.person(self.person_id))
        self.deliver(FakePusher({JO_TOKEN: apns.Result("failed", "TooManyRequests")}))
        self.assertIsNone(self.alert(alert_id).push_due)
        self.assertEqual([t for t, _ in self.events()][-2:], ["push_failed", "alert_failed"])

    def test_a_push_queued_again_while_one_was_out_stays_queued(self):
        alert_id = self.open(alerts.person(self.person_id))
        world = self

        class RequeuingPusher(FakePusher):
            def send(self, tokens, push_):
                with db.session(world.engine) as opened:
                    opened.get(Alert, alert_id).push_due = world.now + 30
                return [apns.Result("sent", "") for _ in tokens]

        self.deliver(RequeuingPusher())
        self.assertEqual(self.alert(alert_id).push_due, self.now + 30)


class LateRevocationTests(PushWorld):
    def targets_then(self, change):
        real = push_tokens.targets

        def wrapped(opened, settings, person_ids, now):
            found = real(opened, settings, person_ids, now)
            with db.session(self.engine) as other:
                change(other)
            return found

        return mock.patch.object(push_tokens, "targets", wrapped)

    def test_a_device_revoked_after_its_token_was_read_is_not_pushed(self):
        self.open(alerts.person(self.person_id))
        pusher = FakePusher()
        with self.targets_then(lambda other: devices.revoke(other, self.device_id, self.now)):
            self.assertEqual(self.deliver(pusher), 0)
        self.assertEqual(pusher.sent, [])
        self.assertEqual([t for t, _ in self.events()][-2:], ["push_skipped", "alert_no_device"])

    def test_an_owner_whose_access_lapsed_after_the_token_was_read_is_skipped_and_the_others_still_get_it(self):
        self.open(alerts.account(self.account()))
        pusher = FakePusher()

        def lapse(other):
            other.get(User, "sub-jo").last_seen = 1.0

        with self.targets_then(lapse):
            self.assertEqual(self.deliver(pusher), 1)
        self.assertEqual(pusher.sent[0][0], [SAM_TOKEN])
        self.assertIn(("push_skipped", "jo"), self.events())
        self.assertIn(("alert_sent", "a@example.test"), self.events())

    def sent_after(self, change, about=None):
        account_id = self.account()
        self.open(about or alerts.account(account_id))
        pusher = FakePusher()
        with self.targets_then(lambda other: change(other, account_id)):
            self.deliver(pusher)
        return [token for tokens, _ in pusher.sent for token in tokens]

    def test_a_person_the_account_stopped_serving_as_its_default_is_skipped(self):
        def move_default(other, account_id):
            other.get(GoogleAccount, account_id).default_person_id = self.sam_id

        self.assertEqual(self.sent_after(move_default), [SAM_TOKEN])
        self.assertIn(("push_skipped", "jo"), self.events())

    def test_a_person_taken_off_the_accounts_pixel_is_skipped(self):
        def unserve(other, account_id):
            other.query(PixelPerson).filter_by(person_id=self.sam_id).delete()

        self.assertEqual(self.sent_after(unserve), [JO_TOKEN])
        self.assertIn(("push_skipped", "sam"), self.events())

    def test_nobody_is_pushed_once_the_account_is_deleted(self):
        def delete(other, account_id):
            other.query(Pixel).filter_by(google_account_id=account_id).delete()
            other.query(GoogleAccount).filter_by(id=account_id).delete()

        self.assertEqual(self.sent_after(delete), [])
        self.assertEqual([t for t, _ in self.events()][-3:], ["push_skipped", "push_skipped", "alert_no_device"])

    def test_a_device_whose_token_was_replaced_is_not_sent_the_old_one(self):
        self.assertEqual(self.sent_after(lambda other, _: push_tokens.register(other, self.settings, self.device_id, MAX_TOKEN, self.now),
                                         about=alerts.person(self.person_id)), [])
        self.assertIn(("push_skipped", "jo"), self.events())

    def test_an_alert_closed_after_its_targets_were_read_is_not_pushed_or_counted_as_undelivered(self):
        self.assertEqual(self.sent_after(lambda other, _: alerts.close(other, "k", self.now), about=alerts.person(self.person_id)), [])
        self.assertNotIn("alert_no_device", [t for t, _ in self.events()])


class BrokenPusherTests(PushWorld):
    def test_a_push_that_raises_is_recorded_as_failed_and_later_alerts_still_go_out(self):
        first = self.open(alerts.person(self.person_id), key="k1")
        second = self.open(alerts.person(self.sam_id), key="k2")

        class BreaksOnFirst(FakePusher):
            def send(self, tokens, push_):
                if push_.alert_id == first:
                    raise ValueError("unexpected")
                return super().send(tokens, push_)

        pusher = BreaksOnFirst()
        with contextlib.redirect_stderr(io.StringIO()) as logged:
            self.assertEqual(self.deliver(pusher), 1)
        self.assertEqual([sent.alert_id for _, sent in pusher.sent], [second])
        self.assertIsNone(self.alert(first).push_due)
        self.assertIn(("alert_failed", "a@example.test"), self.events())
        self.assertIn("ValueError", logged.getvalue())
        self.assertNotIn("unexpected", logged.getvalue())


class NotifierTests(unittest.TestCase):
    def test_alerts_are_queued_for_push_only_when_apns_is_set_up_without_problems(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        key = write_key(Path(folder.name))
        values = {"APNS_KEY_ID": KEY_ID, "APNS_TEAM_ID": TEAM_ID, "APNS_BUNDLE_ID": BUNDLE_ID, "APNS_KEY_FILE": str(key)}
        self.assertIs(alerts.notifier_for(settings_from(**values)), alerts.queued)
        self.assertIs(alerts.notifier_for(settings_from(**(values | {"APNS_KEY_ID": "short"}))), alerts.undelivered)
        self.assertIs(alerts.notifier_for(settings_from()), alerts.undelivered)


class EndToEndTests(PushWorld):
    def test_the_push_job_reaches_apns_with_only_the_kind_id_and_title_and_nothing_private_escapes(self):
        folder = Path(self.root)
        signing = new_key()
        key_file = write_key(folder, signing)
        fake = FakeApns(folder, signing.public_key())
        self.addCleanup(fake.listener.close)
        fake.__enter__()
        client = apns.Apns(apns.Credentials(KEY_ID, TEAM_ID, BUNDLE_ID, key_file), Clock(self.now), host=HOST, port=fake.port,
                           cafile=str(fake.cafile), timeout=5.0)
        alert_id = self.open(alerts.person(self.person_id), subject="jo@example.test")
        with no_leaks(self, JO_TOKEN, SAM_TOKEN, SECRET, database=self.engine):
            self.assertEqual(jobs.push_alerts(self.settings, self.engine, client, self.now), 1)
        self.assertEqual([received.path for received in fake.state.received], [f"/3/device/{JO_TOKEN}"])
        body = json.loads(fake.state.received[0].body)
        self.assertEqual(body, {"aps": {"alert": {"title": alerts.TITLES["sign_in_needed"]}, "sound": "default", "thread-id": "sign_in_needed"},
                                "kind": "sign_in_needed", "alert_id": alert_id})
        self.assertNotIn("example.test", fake.state.received[0].body.decode())

    def test_the_scheduler_pushes_every_minute(self):
        pusher = FakePusher()
        scheduler = jobs.build(self.settings, self.engine, self.now, pusher=pusher)
        self.assertEqual(scheduler.jobs["alerts.push"].interval, jobs.PUSH_TICK_SECONDS)
        self.open(alerts.person(self.person_id))
        scheduler.run_one("alerts.push", self.now)
        self.assertEqual(pusher.sent[0][0], [JO_TOKEN])


if __name__ == "__main__":
    unittest.main()
