import dataclasses
import unittest

from sqlalchemy import select

from darkroom import config
from darkroom.domain import alert_push, browser_jobs
from darkroom.providers import resend
from darkroom.server import jobs
from darkroom.storage import db, secretbox
from darkroom.storage.models import Alert
from tests.browser.test_classify import EMAIL_CANARY, NAME_CANARY, TOKEN_CANARY
from tests.browser.test_download import NOW, DomainReporter, DownloadBrowser, DownloadWorld
from tests.fakes.resend import FakeResend
from tests.privacy import no_leaks
from tests.shared import VIEWER_SECRET, settings_from

SECRET = "test-secret-key"
KEY = "re_canary-key-80c1d4"
FROM = "alerts@mail.example.test"
TO = "owner.canary@example.test"
PUBLIC = "https://darkroom.example"


class PushingReporter(DomainReporter):
    def __init__(self, engine, settings, sender):
        super().__init__(engine, settings)
        self.sender = sender
        self.alerts_seen = []

    def waiting(self, job_id, reason):
        super().waiting(job_id, reason)
        with self.session() as opened:
            self.alerts_seen += [(alert.kind, alert.link, alert.closed) for alert in opened.execute(select(Alert)).scalars()]
        jobs.push_alerts(self.settings, self.engine, self.sender, NOW)


class PromptEmailTests(DownloadWorld):
    def setUp(self):
        super().setUp()
        self.settings = settings_from(DARKROOM_PHOTOS_ROOT=str(self.root), DARKROOM_PUBLIC_URL=PUBLIC, viewer_secret=VIEWER_SECRET, DARKROOM_SECRET_KEY=SECRET,
                                      RESEND_API_KEY=secretbox.seal(KEY, SECRET), ALERT_EMAIL_FROM=FROM, ALERT_EMAIL_TO=TO)
        self.fake = FakeResend(self)
        real = resend.of(self.settings)
        assert real is not None
        self.reporter = PushingReporter(self.engine, self.settings, dataclasses.replace(real, endpoint=self.fake.url))

    def test_a_verify_wait_emails_the_remote_browser_link_and_nothing_from_the_page(self):
        with no_leaks(self, KEY, database=self.engine):
            self.run_job(DownloadBrowser(["ok", "reauth", "ok", "ok"]))
        self.assertEqual(self.reporter.alerts_seen, [("sign_in_needed", config.viewer_link(self.settings), None)])
        self.assertIsNotNone(config.viewer_link(self.settings))
        self.assertEqual(len(self.fake.requests), 1)
        sent = self.fake.requests[0].json()
        self.assertEqual((sent["to"], sent["subject"]), ([TO], "Google sign-in needed"))
        self.assertEqual(sent["text"], f"{alert_push.OPEN_TEXT}\n\n{PUBLIC}/browser")
        everything = self.fake.requests[0].body.decode().replace(TO, "").replace(FROM, "")
        for private in ("jo@example.test", EMAIL_CANARY, NAME_CANARY, TOKEN_CANARY, "accounts.google.com", "takeout", "verify", "part"):
            self.assertNotIn(private, everything)

    def test_the_wrong_account_email_links_to_the_remote_browser_too_and_its_resolved_email_to_the_front_page(self):
        job = self.claim()
        with db.session(self.engine) as opened:
            browser_jobs.waiting(opened, self.settings, job, "wrong_account", NOW)
        jobs.push_alerts(self.settings, self.engine, self.reporter.sender, NOW)
        with db.session(self.engine) as opened:
            browser_jobs.progress(opened, job, "signed in", NOW + 60)
        jobs.push_alerts(self.settings, self.engine, self.reporter.sender, NOW + 60)
        texts = [(received.json()["subject"], received.json()["text"]) for received in self.fake.requests]
        self.assertEqual(texts, [("Wrong Google account", f"{alert_push.OPEN_TEXT}\n\n{PUBLIC}/browser"),
                                 ("Resolved: Wrong Google account", f"{alert_push.CLOSED_TEXT}\n\n{PUBLIC}")])


if __name__ == "__main__":
    unittest.main()
