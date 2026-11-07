import contextlib
import io
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import run
from darkroom import config
from darkroom.domain import alert_push, alerts, events
from darkroom.providers import exiftool, ntfy
from darkroom.providers.push_note import Note
from darkroom.server import jobs
from darkroom.storage import db, secretbox
from darkroom.storage.models import Alert
from tests.privacy import no_leaks
from tests.shared import own_database, settings_from

SECRET = "test-secret-key"
TOPIC = "canary-topic-7c1e9a"
TOKEN = "tk_canary-token-55d0b3"
EMAIL = "canary.person@example.test"
DAY = 24 * 3600.0


class FakeNtfy:
    def __init__(self, test: unittest.TestCase):
        self.requests: list[tuple[str, dict[str, str], bytes]] = []
        self.status = 200
        self.delay = 0.0
        self.location = ""
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake.requests.append((self.path, dict(self.headers.items()), body))
                if fake.delay:
                    time.sleep(fake.delay)
                self.send_response(fake.status)
                if fake.location:
                    self.send_header("Location", fake.location)
                self.send_header("Content-Length", "2")
                self.end_headers()
                with contextlib.suppress(OSError):
                    self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"


def sealed(value: str, secret: str = SECRET) -> str:
    return secretbox.seal(value, secret)


def ntfy_settings(url: str = "https://ntfy.example", topic: str = TOPIC, token: str = "", **more: str) -> config.Settings:
    values = {"DARKROOM_SECRET_KEY": SECRET, "DARKROOM_PUBLIC_URL": "https://d.example", "NTFY_URL": url, "NTFY_TOPIC": sealed(topic)} | more
    if token:
        values["NTFY_TOKEN"] = sealed(token)
    return settings_from(**values)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeNtfy(self)
        self.note = Note("Google sign-in needed", "Open it.", "warning", "https://d.example")

    def test_a_note_is_posted_to_the_topic_with_its_title_tags_link_and_token(self):
        sender = ntfy.Ntfy(self.fake.url, TOPIC, TOKEN)
        self.assertEqual(sender.send(self.note), "sent")
        path, headers, body = self.fake.requests[0]
        self.assertEqual(path, f"/{TOPIC}")
        self.assertEqual((headers["Title"], headers["Tags"], headers["Click"]), ("Google sign-in needed", "warning", "https://d.example"))
        self.assertEqual(headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(body, b"Open it.")

    def test_without_a_token_or_link_neither_header_is_sent(self):
        self.assertEqual(ntfy.Ntfy(self.fake.url, TOPIC).send(Note("T", "m", "warning")), "sent")
        headers = self.fake.requests[0][1]
        self.assertNotIn("Authorization", headers)
        self.assertNotIn("Click", headers)

    def test_server_errors_and_rate_limits_are_retried_and_other_refusals_are_not(self):
        sender = ntfy.Ntfy(self.fake.url, TOPIC, TOKEN)
        for status, outcome in ((500, "retry"), (503, "retry"), (429, "retry"), (401, "failed"), (403, "failed"), (404, "failed")):
            self.fake.status = status
            with self.subTest(status=status), no_leaks(self, TOPIC, TOKEN):
                self.assertEqual(sender.send(self.note), outcome)

    def test_a_redirect_is_not_followed(self):
        self.fake.status, self.fake.location = 302, "http://127.0.0.1:1/elsewhere"
        with no_leaks(self, TOPIC, TOKEN):
            self.assertEqual(ntfy.Ntfy(self.fake.url, TOPIC, TOKEN).send(self.note), "failed")
        self.assertEqual(len(self.fake.requests), 1)

    def test_an_unreachable_or_slow_server_is_retried_without_waiting_long(self):
        self.fake.server.server_close()
        with no_leaks(self, TOPIC, TOKEN):
            self.assertEqual(ntfy.Ntfy(self.fake.url, TOPIC, TOKEN).send(self.note), "retry")
        slow = FakeNtfy(self)
        slow.delay = 2.0
        started = time.monotonic()
        with no_leaks(self, TOPIC, TOKEN):
            self.assertEqual(ntfy.Ntfy(slow.url, TOPIC, TOKEN, timeout=0.3).send(self.note), "retry")
        self.assertLess(time.monotonic() - started, 1.5)

    def test_plain_http_is_refused_beyond_the_lan(self):
        with no_leaks(self, TOPIC, TOKEN):
            self.assertEqual(ntfy.Ntfy("http://ntfy.example.com", TOPIC, TOKEN).send(self.note), "failed")

    def test_the_topic_and_token_stay_out_of_its_repr(self):
        shown = repr(ntfy.Ntfy(self.fake.url, TOPIC, TOKEN))
        self.assertNotIn(TOPIC, shown)
        self.assertNotIn(TOKEN, shown)


class SettingsTests(unittest.TestCase):
    def test_unset_means_no_problems_and_alerts_stay_undelivered(self):
        settings = settings_from(DARKROOM_SECRET_KEY=SECRET)
        self.assertEqual(config.ntfy_problems(settings) + ntfy.problems(settings), [])
        self.assertIsNone(ntfy.of(settings))
        self.assertIs(alerts.notifier_for(settings), alerts.undelivered)

    def test_good_settings_give_a_sender_and_queue_alerts(self):
        settings = ntfy_settings(token=TOKEN)
        self.assertEqual(config.ntfy_problems(settings) + ntfy.problems(settings), [])
        sender = ntfy.of(settings)
        assert sender is not None
        self.assertEqual((sender.server, sender.topic, sender.token), ("https://ntfy.example", TOPIC, TOKEN))
        self.assertIs(alerts.notifier_for(settings), alerts.queued)

    def test_the_sealed_prefix_is_secretboxes(self):
        self.assertEqual(config.SEALED_PREFIX, secretbox.PREFIX)

    def test_each_bad_setting_is_reported_without_its_value(self):
        cases = {
            "go together": settings_from(NTFY_URL="https://ntfy.example"),
            "must be the ntfy server's address": ntfy_settings(url="https://user:pw@ntfy.example"),
            "without a topic, username, password or query": ntfy_settings(url="https://ntfy.example?topic=x"),
            "must use https:// unless": ntfy_settings(url="http://ntfy.example.com"),
            "server's address, like": ntfy_settings(url="https://ntfy.example:bad"),
            "address, like https": ntfy_settings(url="https://ntfy.example:70000"),
            "NTFY_TOPIC must be sealed": settings_from(DARKROOM_SECRET_KEY=SECRET, NTFY_URL="https://ntfy.example", NTFY_TOPIC=TOPIC),
            "NTFY_TOKEN must be sealed": settings_from(DARKROOM_SECRET_KEY=SECRET, NTFY_URL="https://ntfy.example", NTFY_TOPIC=sealed(TOPIC), NTFY_TOKEN=TOKEN),
        }
        for expected, settings in cases.items():
            with self.subTest(expected=expected):
                found = config.ntfy_problems(settings)
                self.assertTrue(any(expected in line for line in found), found)
                self.assertFalse(any(TOPIC in line or TOKEN in line for line in found))
                self.assertIsNone(ntfy.of(settings))
                self.assertIs(alerts.notifier_for(settings), alerts.undelivered)
        self.assertEqual(config.ntfy_problems(ntfy_settings(url="http://unraid.lan:8080")), [])

    def test_values_that_cannot_be_unsealed_or_are_not_a_topic_are_reported(self):
        other_key = settings_from(DARKROOM_SECRET_KEY="another-key", NTFY_URL="https://ntfy.example", NTFY_TOPIC=sealed(TOPIC))
        self.assertIn("cannot be read with this DARKROOM_SECRET_KEY", ntfy.problems(other_key)[0])
        self.assertIn("must be an ntfy topic name", ntfy.problems(ntfy_settings(topic="not a topic/x"))[0])
        self.assertIn("without spaces", ntfy.problems(ntfy_settings(token="has a space"))[0])
        for settings in (other_key, ntfy_settings(topic="not a topic/x")):
            self.assertIsNone(ntfy.of(settings))


class CommandTests(unittest.TestCase):
    def test_seal_secret_seals_what_it_reads_with_the_secret_key(self):
        with mock.patch.dict(os.environ, {"DARKROOM_SECRET_KEY": SECRET}, clear=True), mock.patch("sys.stdin", io.StringIO(TOPIC + "\n")), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run.main(["seal-secret"]), 0)
        self.assertEqual(secretbox.unseal(out.getvalue().strip(), SECRET), TOPIC)
        self.assertNotIn(TOPIC, out.getvalue())

    def test_seal_secret_needs_the_key_and_a_value(self):
        for environ, given, expected in (({}, TOPIC, "needs DARKROOM_SECRET_KEY"), ({"DARKROOM_SECRET_KEY": SECRET}, "", "reads the value")):
            with mock.patch.dict(os.environ, environ, clear=True), mock.patch("sys.stdin", io.StringIO(given)), \
                    contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(run.main(["seal-secret"]), 1)
            self.assertIn(expected, err.getvalue())

    def check_config(self, **ntfy_values: str) -> tuple[int, str, str]:
        with tempfile.TemporaryDirectory() as folder:
            values = {"DARKROOM_PHOTOS_ROOT": folder, "DARKROOM_DATA_DIR": folder, "DARKROOM_SECRET_KEY": SECRET, "DARKROOM_PUBLIC_URL": "https://d.example",
                      "DARKROOM_ALLOW_NO_AUTH": "1"} | ntfy_values
            with no_leaks(self, TOPIC, TOKEN), contextlib.redirect_stderr(io.StringIO()) as err, contextlib.redirect_stdout(io.StringIO()) as out, \
                    mock.patch.dict(os.environ, values, clear=True), mock.patch.object(exiftool, "version", return_value="13.0"):
                code = run.main(["check-config"])
        return code, out.getvalue(), err.getvalue()

    def test_check_config_says_where_alerts_go_without_showing_the_topic_or_token(self):
        code, out, _ = self.check_config()
        self.assertEqual(code, 0)
        self.assertIn("Alerts: shown on the Status page only (set NTFY_URL", out)
        code, out, _ = self.check_config(NTFY_URL="https://ntfy.example/base", NTFY_TOPIC=sealed(TOPIC), NTFY_TOKEN=sealed(TOKEN))
        self.assertEqual(code, 0)
        self.assertIn("pushed through the ntfy server at ntfy.example, with an access token", out)
        self.assertNotIn("/base", out)
        code, out, err = self.check_config(NTFY_URL="https://ntfy.example", NTFY_TOPIC=TOPIC)
        self.assertEqual(code, 1)
        self.assertIn("NTFY_TOPIC must be sealed", err)
        self.assertIn("need fixing", out)
        code, _, err = self.check_config(NTFY_URL="https://ntfy.example", NTFY_TOPIC=sealed(TOPIC, "old-key"))
        self.assertEqual(code, 1)
        self.assertIn("cannot be read with this DARKROOM_SECRET_KEY", err)


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
        self.settings = ntfy_settings()

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
        self.assertEqual(pusher.notes, [Note("Google sign-in needed", alert_push.OPEN_TEXT, "warning", "https://d.example")])
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
        self.assertEqual((pusher.notes[-1].message, pusher.notes[-1].tags), (alert_push.CLOSED_TEXT, "white_check_mark"))
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
        with no_leaks(self, TOPIC):
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
        with no_leaks(self, TOPIC):
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
        settings = settings_from(DARKROOM_SECRET_KEY=SECRET, NTFY_URL="https://ntfy.example", NTFY_TOPIC=TOPIC)
        with no_leaks(self, TOPIC), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertIsNone(jobs.pusher_of(settings))
        self.assertIn("ntfy is set up but unusable", err.getvalue())


class EndToEndTests(unittest.TestCase):
    def test_an_alert_reaches_a_fake_ntfy_server_with_nothing_private_and_nothing_leaks(self):
        fake = FakeNtfy(self)
        engine = own_database(self)
        settings = ntfy_settings(url=fake.url, token=TOKEN)
        sender = ntfy.of(settings)
        with no_leaks(self, TOPIC, TOKEN, database=engine):
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
        for path, headers, body in fake.requests:
            self.assertEqual(path, f"/{TOPIC}")
            self.assertNotIn(EMAIL, body.decode() + repr(headers))
        self.assertEqual(fake.requests[1][1]["Title"], "Resolved: Google sign-in needed")
        with db.session(engine) as opened:
            self.assertEqual([e.type for e in reversed(events.recent(opened))], ["alert_sent", "alert_sent"])
            self.assertEqual(opened.query(Alert).filter(Alert.key == "sign-in:b").one().push_due, 40.0 + alert_push.RETRY_AFTER)

    def test_the_demo_never_posts_to_a_real_topic_not_even_a_close_note(self):
        fake = FakeNtfy(self)
        engine = own_database(self)
        settings = ntfy_settings(url=fake.url, DARKROOM_DEMO="1", DARKROOM_HOST="127.0.0.1")
        self.assertIsNotNone(ntfy.of(settings))
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
        self.assertIsNone(jobs.pusher_of(settings_from(DARKROOM_DEMO="1", DARKROOM_SECRET_KEY=SECRET, NTFY_URL=fake.url, NTFY_TOPIC=sealed(TOPIC))))
        with db.session(engine) as opened:
            self.assertTrue(all(alert.push_due is None for alert in opened.query(Alert)))

    def test_an_alert_closed_before_its_push_sends_only_the_resolved_note(self):
        engine = own_database(self)
        settings = ntfy_settings()
        pusher = FakePusher()
        with db.session(engine) as opened:
            alerts.open_alert(opened, settings, "sign-in:a", "sign_in_needed", EMAIL, alerts.sign_in_needed(EMAIL), None, 0.0, about=alerts.NOBODY)
            alerts.close(opened, "sign-in:a", 5.0)
        jobs.push_alerts(settings, engine, pusher, 10.0)
        self.assertEqual([note.title for note in pusher.notes], ["Resolved: Google sign-in needed"])
