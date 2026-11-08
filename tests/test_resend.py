import contextlib
import dataclasses
import io
import os
import tempfile
import time
import unittest
from unittest import mock

import run
from darkroom import config
from darkroom.domain import alert_push, alerts, events
from darkroom.providers import exiftool, resend
from darkroom.providers.push_note import Note
from darkroom.server import jobs
from darkroom.storage import db, secretbox
from darkroom.storage.models import Alert
from tests.fakes.resend import FakeResend
from tests.privacy import no_leaks
from tests.shared import own_database, settings_from

SECRET = "test-secret-key"
KEY = "re_canary-key-55d0b3"
FROM = "alerts@mail.example.test"
TO = "owner.canary@example.test"
EMAIL = "canary.person@example.test"
SUBJECT_TEXT = "Import 3 for canary.person"
DAY = 24 * 3600.0


def sealed(value: str, secret: str = SECRET) -> str:
    return secretbox.seal(value, secret)


def resend_settings(key: str = KEY, sender: str = FROM, to: str = TO, **more: str) -> config.Settings:
    values = {"DARKROOM_SECRET_KEY": SECRET, "DARKROOM_PUBLIC_URL": "https://d.example", "RESEND_API_KEY": sealed(key), "ALERT_EMAIL_FROM": sender,
              "ALERT_EMAIL_TO": to} | more
    return settings_from(**values)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeResend(self)
        self.note = Note("Google sign-in needed", "Open it.", "https://d.example")
        self.sender = resend.Resend(KEY, FROM, TO, self.fake.url)

    def test_a_note_is_posted_as_one_email_with_the_key_and_a_link(self):
        self.assertEqual(self.sender.send(self.note), "sent")
        received = self.fake.requests[0]
        self.assertEqual(received.path, "/emails")
        self.assertEqual(received.headers["Authorization"], f"Bearer {KEY}")
        self.assertEqual(received.headers["Content-Type"], "application/json")
        self.assertEqual(received.json(), {"from": FROM, "to": [TO], "subject": "Google sign-in needed", "text": "Open it.\n\nhttps://d.example"})

    def test_without_a_link_the_text_is_the_message_alone(self):
        self.assertEqual(self.sender.send(Note("T", "m")), "sent")
        self.assertEqual(self.fake.requests[0].json()["text"], "m")

    def test_the_production_endpoint_is_resends_https_api(self):
        self.assertEqual(resend.Resend(KEY, FROM, TO).endpoint, "https://api.resend.com/emails")

    def test_server_errors_and_rate_limits_are_retried_and_other_refusals_are_not(self):
        for status, outcome in ((500, "retry"), (503, "retry"), (429, "retry"), (400, "failed"), (401, "failed"), (403, "failed"), (422, "failed")):
            self.fake.status = status
            with self.subTest(status=status), no_leaks(self, KEY, FROM, TO):
                self.assertEqual(self.sender.send(self.note), outcome)

    def test_a_redirect_is_not_followed(self):
        self.fake.status, self.fake.location = 302, "http://127.0.0.1:1/elsewhere"
        with no_leaks(self, KEY, FROM, TO):
            self.assertEqual(self.sender.send(self.note), "failed")
        self.assertEqual(len(self.fake.requests), 1)

    def test_an_unreachable_or_slow_server_is_retried_without_waiting_long(self):
        self.fake.server.server_close()
        with no_leaks(self, KEY, FROM, TO):
            self.assertEqual(self.sender.send(self.note), "retry")
        slow = FakeResend(self)
        slow.delay = 2.0
        started = time.monotonic()
        with no_leaks(self, KEY, FROM, TO):
            self.assertEqual(resend.Resend(KEY, FROM, TO, slow.url, timeout=0.3).send(self.note), "retry")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_plain_http_is_refused_beyond_the_lan(self):
        with no_leaks(self, KEY, FROM, TO):
            self.assertEqual(resend.Resend(KEY, FROM, TO, "http://api.resend.example.com/emails").send(self.note), "failed")

    def test_the_key_and_addresses_stay_out_of_its_repr(self):
        shown = repr(self.sender)
        for canary in (KEY, FROM, TO):
            self.assertNotIn(canary, shown)


class SettingsTests(unittest.TestCase):
    def test_unset_means_no_problems_and_alerts_stay_undelivered(self):
        settings = settings_from(DARKROOM_SECRET_KEY=SECRET)
        self.assertEqual(config.resend_problems(settings) + resend.problems(settings), [])
        self.assertIsNone(resend.of(settings))
        self.assertIs(alerts.notifier_for(settings), alerts.undelivered)

    def test_good_settings_give_a_sender_and_queue_alerts(self):
        settings = resend_settings()
        self.assertEqual(config.resend_problems(settings) + resend.problems(settings), [])
        sender = resend.of(settings)
        assert sender is not None
        self.assertEqual((sender.api_key, sender.sender, sender.recipient), (KEY, FROM, TO))
        self.assertIs(alerts.notifier_for(settings), alerts.queued)

    def test_leftover_ntfy_variables_are_reported_without_their_values_and_stop_nothing(self):
        canary = "https://ntfy.canary.example"
        with mock.patch.dict(os.environ, {"NTFY_URL": canary}, clear=True):
            self.assertTrue(config.ignored_alert_variables())
            with no_leaks(self, canary), contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertIsNone(jobs.pusher_of(settings_from(DARKROOM_SECRET_KEY=SECRET)))
        self.assertIn('"level": "warning"', err.getvalue())
        self.assertIn("RESEND_API_KEY", err.getvalue())
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(config.ignored_alert_variables())

    def test_the_sealed_prefix_is_secretboxes(self):
        self.assertEqual(config.SEALED_PREFIX, secretbox.PREFIX)

    def test_each_bad_setting_is_reported_without_its_value(self):
        cases = {
            "go together": settings_from(RESEND_API_KEY=sealed(KEY)),
            "go together, partly": settings_from(DARKROOM_SECRET_KEY=SECRET, ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO),
            "RESEND_API_KEY must be sealed": settings_from(DARKROOM_SECRET_KEY=SECRET, RESEND_API_KEY=KEY, ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO),
            "ALERT_EMAIL_FROM must be one email address": resend_settings(sender="not an address"),
            "ALERT_EMAIL_TO must be one email address": resend_settings(to="a@example.test, b@example.test"),
            "ALERT_EMAIL_TO must be one email address, ": resend_settings(to="Owner <owner@example.test>"),
        }
        for expected, settings in cases.items():
            with self.subTest(expected=expected):
                found = config.resend_problems(settings)
                self.assertTrue(any(expected.removesuffix(", partly").removesuffix(", ") in line for line in found), found)
                self.assertFalse(any(KEY in line or FROM in line or TO in line for line in found))
                self.assertIsNone(resend.of(settings))
                self.assertIs(alerts.notifier_for(settings), alerts.undelivered)

    def test_a_key_that_cannot_be_unsealed_or_is_not_a_key_is_reported(self):
        other_key = settings_from(DARKROOM_SECRET_KEY="another-key", RESEND_API_KEY=sealed(KEY), ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.assertIn("cannot be read with this DARKROOM_SECRET_KEY", resend.problems(other_key)[0])
        self.assertIn("without spaces", resend.problems(resend_settings(key="has a space"))[0])
        no_secret = settings_from(RESEND_API_KEY=sealed(KEY), ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.assertIn("DARKROOM_SECRET_KEY is not", resend.problems(no_secret)[0])
        for settings in (other_key, resend_settings(key="has a space"), no_secret):
            self.assertIsNone(resend.of(settings))


class CommandTests(unittest.TestCase):
    def test_seal_secret_seals_what_it_reads_with_the_secret_key(self):
        with mock.patch.dict(os.environ, {"DARKROOM_SECRET_KEY": SECRET}, clear=True), mock.patch("sys.stdin", io.StringIO(KEY + "\n")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["seal-secret"]), 0)
        self.assertEqual(secretbox.unseal(out.getvalue().strip(), SECRET), KEY)
        self.assertNotIn(KEY, out.getvalue())

    def test_seal_secret_needs_the_key_and_a_value(self):
        for environ, given, expected in (({}, KEY, "needs DARKROOM_SECRET_KEY"), ({"DARKROOM_SECRET_KEY": SECRET}, "", "reads the value")):
            with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdin", io.StringIO(given)), \
                    contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(["seal-secret"]), 1)
            self.assertIn(expected, err.getvalue())

    def check_config(self, **resend_values: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": SECRET, "DARKROOM_PUBLIC_URL": "https://d.example",
                      "DARKROOM_ALLOW_NO_AUTH": "1"} | resend_values
            with no_leaks(self, KEY, FROM, TO), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out, \
                    mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                code = run.main(["check-config"])
        return code, out.getvalue(), err.getvalue()

    def test_check_config_says_where_alerts_go_without_showing_the_key_or_addresses(self):
        code, out, _ = self.check_config()
        self.assertEqual(code, 0)
        self.assertIn("Alerts: shown on the Status page only (set RESEND_API_KEY", out)
        code, out, _ = self.check_config(RESEND_API_KEY=sealed(KEY), ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.assertEqual(code, 0)
        self.assertIn("emailed to the owner through Resend", out)
        code, out, err = self.check_config(RESEND_API_KEY=KEY, ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.assertEqual(code, 1)
        self.assertIn("RESEND_API_KEY must be sealed", err)
        self.assertIn("need fixing", out)
        code, _, err = self.check_config(RESEND_API_KEY=sealed(KEY, "old-key"), ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.assertEqual(code, 1)
        self.assertIn("cannot be read with this DARKROOM_SECRET_KEY", err)
        code, _, err = self.check_config(NTFY_TOPIC="canary-topic")
        self.assertEqual(code, 0)
        self.assertIn("NTFY_URL, NTFY_TOPIC or NTFY_TOKEN is set and ignored", err)
        self.assertNotIn("canary-topic", err)
        code, _, err = self.check_config(RESEND_API_KEY=sealed(KEY))
        self.assertEqual(code, 1)
        self.assertIn("go together", err)


class FakePusher:
    def __init__(self, *outcomes: str):
        self.outcomes = list(outcomes)
        self.notes: list[Note] = []
        self.during = None

    def send(self, note: Note) -> str:
        self.notes.append(note)
        if self.during is not None:
            self.during()
        if not self.outcomes:
            return "sent"
        outcome = self.outcomes.pop(0)
        if outcome == "raise":
            raise RuntimeError("boom")
        return outcome


class PushPassTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.settings = resend_settings()

    def open_sign_in(self, now: float, key: str = "sign-in:a") -> None:
        with db.session(self.engine) as opened:
            alerts.open_alert(opened, self.settings, key, "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), "https://d.example/browser", now, about=alerts.NOBODY)

    def push(self, pusher, now: float) -> int:
        with db.session(self.engine) as opened:
            return alert_push.deliver_due(opened, self.settings, pusher, now)

    def recorded(self) -> list[tuple[str, str | None]]:
        with db.session(self.engine) as opened:
            return [(e.type, e.detail) for e in reversed(events.recent(opened))]

    def alert(self) -> Alert:
        with db.session(self.engine) as opened:
            found = opened.query(Alert).one()
            opened.expunge(found)
            return found

    def test_an_alert_is_queued_then_pushed_once_with_only_its_fixed_title(self):
        pusher = FakePusher()
        self.open_sign_in(0.0)
        self.assertEqual(self.recorded(), [])
        self.assertEqual(self.alert().push_due, 0.0)
        self.assertEqual(self.push(pusher, 30.0), 1)
        self.assertEqual(self.push(pusher, 90.0), 0)
        self.assertEqual(pusher.notes, [Note("Google sign-in needed", alert_push.OPEN_TEXT, "https://d.example")])
        self.assertNotIn(EMAIL, repr(pusher.notes))
        self.assertEqual(self.recorded(), [("alert_sent", "sign_in_needed")])
        self.assertIsNone(self.alert().push_due)

    def test_repeats_follow_the_repeat_interval_and_closing_sends_one_resolved_note(self):
        pusher = FakePusher()
        self.open_sign_in(0.0)
        self.push(pusher, 1.0)
        with db.session(self.engine) as opened:
            self.assertEqual(alerts.tick(opened, self.settings, DAY - 1), 0)
            self.assertEqual(alerts.tick(opened, self.settings, DAY), 1)
        self.push(pusher, DAY + 1)
        with db.session(self.engine) as opened:
            self.assertEqual(alerts.close(opened, "sign-in:a", DAY + 10), 1)
        self.push(pusher, DAY + 20)
        self.push(pusher, DAY + 80)
        self.assertEqual([note.title for note in pusher.notes], ["Google sign-in needed", "Google sign-in needed", "Resolved: Google sign-in needed"])
        self.assertEqual(pusher.notes[-1].message, alert_push.CLOSED_TEXT)
        self.assertEqual(self.recorded(), [("alert_sent", "sign_in_needed"), ("alert_sent", "sign_in_needed"), ("alert_sent", "sign_in_needed, resolved")])

    def test_one_shot_alerts_are_pushed_once_with_no_close_note(self):
        pusher = FakePusher()
        with db.session(self.engine) as opened:
            alerts.open_alert_committed(opened, self.settings, "failure:1", "failure", "Import 3", alerts.failure("Import 3"), None, 0.0, about=alerts.NOBODY)
            alerts.open_alert(opened, self.settings, "imported:1", "export_imported", "jo", alerts.export_imported("3 added"), None, 0.0, about=alerts.NOBODY)
            self.assertEqual(alerts.close(opened, "failure:1", 5.0), 0)
        self.assertEqual(self.push(pusher, 10.0), 2)
        self.assertEqual(sorted(note.title for note in pusher.notes), ["Darkroom needs a look", "Takeout import finished"])
        self.assertEqual(self.push(pusher, 100.0), 0)

    def test_an_alert_replaced_by_another_kind_sends_no_resolved_note(self):
        pusher = FakePusher()
        self.open_sign_in(0.0)
        self.push(pusher, 1.0)
        with db.session(self.engine) as opened:
            alerts.close_other_kinds(opened, "sign-in:a", "wrong_account", 5.0)
        self.assertEqual(self.push(pusher, 10.0), 0)
        self.assertEqual(len(pusher.notes), 1)

    def test_an_alert_replaced_while_still_queued_sends_nothing_for_the_replaced_kind(self):
        pusher = FakePusher()
        self.open_sign_in(0.0)
        with db.session(self.engine) as opened:
            alerts.close_other_kinds(opened, "sign-in:a", "wrong_account", 5.0)
        self.assertEqual(self.push(pusher, 10.0), 0)
        self.assertEqual(pusher.notes, [])
        self.assertIsNone(self.alert().push_due)

    def test_a_crash_while_a_close_is_queued_meanwhile_keeps_the_close_note(self):
        pusher = FakePusher("raise")
        self.open_sign_in(0.0)

        def close_meanwhile():
            with db.session(self.engine) as other:
                alerts.close(other, "sign-in:a", 2.0)
                other.commit()

        pusher.during = close_meanwhile
        with no_leaks(self, KEY):
            self.push(pusher, 1.0)
        pusher.during = None
        self.assertEqual(self.alert().push_due, 2.0)
        self.assertEqual(self.recorded(), [])
        self.push(pusher, 3.0)
        self.assertEqual(pusher.notes[-1].title, "Resolved: Google sign-in needed")

    def test_an_unreachable_server_is_retried_every_five_minutes_for_an_hour_then_recorded(self):
        pusher = FakePusher(*["retry"] * 20)
        self.open_sign_in(0.0)
        self.assertEqual(self.push(pusher, 0.0), 0)
        self.assertEqual(self.recorded(), [])
        self.assertEqual(self.alert().push_due, alert_push.RETRY_AFTER)
        self.assertEqual(self.push(pusher, 100.0), 0)
        self.assertEqual(len(pusher.notes), 1)
        at = 0.0
        while self.alert().push_due is not None:
            at = self.alert().push_due
            self.push(pusher, at)
        self.assertLessEqual(at, alert_push.RETRY_FOR)
        self.assertEqual(len(pusher.notes), 1 + int(alert_push.RETRY_FOR // alert_push.RETRY_AFTER))
        self.assertEqual(self.recorded(), [("alert_failed", "sign_in_needed")])

    def test_a_retry_that_then_succeeds_is_recorded_as_sent(self):
        pusher = FakePusher("retry")
        self.open_sign_in(0.0)
        self.push(pusher, 0.0)
        self.assertEqual(self.push(pusher, alert_push.RETRY_AFTER), 1)
        self.assertEqual(self.recorded(), [("alert_sent", "sign_in_needed")])

    def test_a_refusal_is_recorded_at_once_and_the_next_repeat_tries_again(self):
        pusher = FakePusher("failed")
        self.open_sign_in(0.0)
        self.push(pusher, 0.0)
        self.assertEqual(self.recorded(), [("alert_failed", "sign_in_needed")])
        self.assertIsNone(self.alert().push_due)
        with db.session(self.engine) as opened:
            alerts.tick(opened, self.settings, DAY)
        self.assertEqual(self.push(pusher, DAY), 1)

    def test_a_crash_while_sending_is_recorded_and_the_pass_goes_on(self):
        pusher = FakePusher("raise", "sent")
        self.open_sign_in(0.0, "sign-in:a")
        self.open_sign_in(1.0, "sign-in:b")
        with no_leaks(self, KEY):
            self.assertEqual(self.push(pusher, 5.0), 1)
        self.assertEqual(sorted(kind for kind, _ in self.recorded()), ["alert_failed", "alert_sent"])
        self.assertEqual(self.push(pusher, 100.0), 0)

    def test_a_close_during_the_send_keeps_its_resolved_note_queued(self):
        pusher = FakePusher("retry")
        self.open_sign_in(0.0)

        def close_meanwhile():
            with db.session(self.engine) as other:
                alerts.close(other, "sign-in:a", 2.0)
                other.commit()

        pusher.during = close_meanwhile
        self.push(pusher, 1.0)
        pusher.during = None
        self.assertEqual(self.alert().push_due, 2.0)
        self.push(pusher, 3.0)
        self.assertEqual(pusher.notes[-1].title, "Resolved: Google sign-in needed")

    def test_with_the_settings_removed_a_queued_alert_is_recorded_disabled_and_a_close_note_dropped(self):
        self.open_sign_in(0.0)
        self.assertEqual(self.push(None, 1.0), 0)
        self.assertEqual(self.recorded(), [("alert_disabled", "sign_in_needed")])
        with db.session(self.engine) as opened:
            alerts.close(opened, "sign-in:a", 5.0)
        self.push(None, 6.0)
        self.assertEqual(self.recorded(), [("alert_disabled", "sign_in_needed")])
        self.assertIsNone(self.alert().push_due)

    def test_unconfigured_alerts_are_recorded_disabled_at_once_as_before(self):
        self.settings = settings_from(DARKROOM_SECRET_KEY=SECRET)
        self.open_sign_in(0.0)
        self.assertEqual(self.recorded(), [("alert_disabled", "sign_in_needed")])
        self.assertIsNone(self.alert().push_due)

    def test_the_push_job_runs_every_minute_in_its_own_thread(self):
        pusher = FakePusher()
        scheduler = jobs.build(self.settings, self.engine, pusher=pusher)
        self.assertEqual(scheduler.jobs["alerts.push"].interval, 60.0)
        self.assertTrue(scheduler.jobs["alerts.push"].own_thread)
        self.open_sign_in(0.0)
        with contextlib.redirect_stderr(io.StringIO()):
            scheduler.run_one("alerts.push", 10.0)
        self.assertEqual(len(pusher.notes), 1)

    def test_unusable_settings_are_logged_once_at_startup_without_their_values(self):
        settings = settings_from(DARKROOM_SECRET_KEY=SECRET, RESEND_API_KEY=KEY, ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        with no_leaks(self, KEY, FROM, TO), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(jobs.pusher_of(settings))
        self.assertIn("Resend is set up but unusable", err.getvalue())


class EndToEndTests(unittest.TestCase):
    def test_an_alert_reaches_a_fake_resend_with_nothing_private_and_nothing_leaks(self):
        fake = FakeResend(self)
        engine = own_database(self)
        settings = resend_settings()
        real = resend.of(settings)
        assert real is not None
        sender = dataclasses.replace(real, endpoint=fake.url)
        with no_leaks(self, KEY, FROM, TO, database=engine):
            with db.session(engine) as opened:
                alerts.open_alert(opened, settings, "sign-in:a", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 0.0, about=alerts.NOBODY)
            jobs.push_alerts(settings, engine, sender, 1.0)
            with db.session(engine) as opened:
                alerts.close(opened, "sign-in:a", 5.0)
            jobs.push_alerts(settings, engine, sender, 10.0)
            jobs.push_alerts(settings, engine, sender, 20.0)
            fake.status = 503
            with db.session(engine) as opened:
                alerts.open_alert(opened, settings, "sign-in:b", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 30.0, about=alerts.NOBODY)
            jobs.push_alerts(settings, engine, sender, 40.0)
        self.assertEqual(len(fake.requests), 3)
        for received in fake.requests:
            self.assertEqual(received.path, "/emails")
            self.assertEqual(received.headers["Authorization"], f"Bearer {KEY}")
            self.assertEqual(received.json()["to"], [TO])
            self.assertNotIn(EMAIL, received.body.decode() + repr(received.headers))
        self.assertEqual(fake.requests[1].json()["subject"], "Resolved: Google sign-in needed")
        with db.session(engine) as opened:
            self.assertEqual([e.type for e in reversed(events.recent(opened))], ["alert_sent", "alert_sent"])
            self.assertEqual(opened.query(Alert).filter(Alert.key == "sign-in:b").one().push_due, 40.0 + alert_push.RETRY_AFTER)

    def test_the_email_carries_none_of_the_alerts_free_text(self):
        fake = FakeResend(self)
        engine = own_database(self)
        settings = resend_settings()
        real = resend.of(settings)
        assert real is not None
        wordings = {"sign-in:a": ("sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL)), "failure:1": ("failure", SUBJECT_TEXT, alerts.failure(SUBJECT_TEXT))}
        with db.session(engine) as opened:
            for key, (kind, subject, wording) in wordings.items():
                alerts.open_alert(opened, settings, key, kind, subject, wording, None, 0.0, about=alerts.NOBODY)
            alerts.close(opened, "sign-in:a", 2.0)
        jobs.push_alerts(settings, engine, dataclasses.replace(real, endpoint=fake.url), 10.0)
        self.assertEqual(len(fake.requests), 2)
        sent = {received.json()["subject"]: str(received.json()["text"]) for received in fake.requests}
        self.assertEqual(set(sent), {"Resolved: Google sign-in needed", "Darkroom needs a look"})
        for received in fake.requests:
            everything = received.body.decode()
            for _, subject, wording in wordings.values():
                for private in (subject, wording.message, "canary.person", "example.test"):
                    self.assertNotIn(private, everything.replace(TO, "").replace(FROM, ""))
        self.assertEqual(sent["Darkroom needs a look"], f"{alert_push.OPEN_TEXT}\n\nhttps://d.example")
        self.assertEqual(sent["Resolved: Google sign-in needed"], f"{alert_push.CLOSED_TEXT}\n\nhttps://d.example")

    def test_the_demo_never_posts_to_a_real_address_not_even_a_close_note(self):
        fake = FakeResend(self)
        engine = own_database(self)
        settings = resend_settings(DARKROOM_DEMO="1", DARKROOM_HOST="127.0.0.1")
        self.assertIsNotNone(resend.of(settings))
        scheduler = jobs.build(settings, engine)
        with db.session(engine) as opened:
            alerts.open_alert(opened, settings, "sign-in:a", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 0.0, about=alerts.NOBODY)
            alerts.close(opened, "sign-in:a", 5.0)
        scheduler.run_one("alerts.push", 10.0)
        with db.session(engine) as opened:
            alerts.open_alert(opened, settings, "sign-in:b", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 20.0, about=alerts.NOBODY)
            alerts.tick(opened, settings, DAY + 20.0)
            alerts.close(opened, "sign-in:b", DAY + 30.0)
        scheduler.run_one("alerts.push", DAY + 40.0)
        self.assertEqual(fake.requests, [])
        self.assertIsNone(jobs.pusher_of(resend_settings(DARKROOM_DEMO="1")))
        with db.session(engine) as opened:
            self.assertTrue(all(alert.push_due is None for alert in opened.query(Alert)))

    def test_an_alert_closed_before_its_push_sends_only_the_resolved_note(self):
        engine = own_database(self)
        settings = resend_settings()
        pusher = FakePusher()
        with db.session(engine) as opened:
            alerts.open_alert(opened, settings, "sign-in:a", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 0.0, about=alerts.NOBODY)
            alerts.close(opened, "sign-in:a", 5.0)
        jobs.push_alerts(settings, engine, pusher, 10.0)
        self.assertEqual([note.title for note in pusher.notes], ["Resolved: Google sign-in needed"])
