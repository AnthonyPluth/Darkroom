import dataclasses
import os
import unittest
from unittest import mock

from sqlalchemy import select

from darkroom import config
from darkroom.domain import alerts, events, pixel_check, share_check, syncthing_version, transport, transport_status
from darkroom.storage import db
from darkroom.storage.models import Alert, Pixel, PixelCopy, PixelTransport
from tests import test_pixel_folder
from tests.privacy import no_leaks
from tests.shared import own_database

HOUR = 3600.0
DAY = 24 * HOUR


class Notifier:
    def __init__(self, outcome=alerts.SENT):
        self.sent = []
        self.texts = []
        self.outcome = outcome

    def __call__(self, settings, title, message, url):
        self.sent.append((title, message, url))
        self.texts.append(f"{title} {message} {url}".encode())
        return self.outcome


class AlertTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.settings = config.load()
        self.notifier = Notifier()

    def open_sign_in(self, opened, now):
        return alerts.open_alert(opened, self.settings, "sign-in:a", "sign_in_needed", "a@x.test", alerts.sign_in_needed("a@x.test"),
                                 "https://d.example/browser", now, notifier=self.notifier, about=alerts.NOBODY)

    def tick(self, now):
        with db.session(self.engine) as opened:
            return alerts.tick(opened, self.settings, now, self.notifier)

    def test_a_kept_open_alert_is_sent_at_0_24h_and_4d_and_never_again(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.assertEqual(len(self.notifier.sent), 1)
        self.assertEqual(self.notifier.sent[0][2], "https://d.example/browser")
        self.assertEqual(self.tick(DAY - 1), 0)
        self.assertEqual(self.tick(DAY), 1)
        self.assertEqual(self.tick(DAY + HOUR), 0)
        self.assertEqual(self.tick(4 * DAY - 1), 0)
        self.assertEqual(self.tick(4 * DAY), 1)
        self.assertEqual(self.tick(40 * DAY), 0)
        self.assertEqual(len(self.notifier.sent), 3)

    def test_opening_the_same_alert_twice_sends_once(self):
        with db.session(self.engine) as opened:
            first = self.open_sign_in(opened, 0.0)
            second = self.open_sign_in(opened, 5.0)
            self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.notifier.sent), 1)

    def test_close_stops_repeats_and_reopening_starts_over(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        with db.session(self.engine) as opened:
            self.assertEqual(alerts.close(opened, "sign-in:a", 10.0), 1)
            self.assertEqual(alerts.close(opened, "sign-in:a", 11.0), 0)
        self.assertEqual(self.tick(2 * DAY), 0)
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 3 * DAY)
        self.assertEqual(len(self.notifier.sent), 2)
        self.assertEqual(self.tick(4 * DAY), 1)

    def test_one_shot_alerts_are_closed_at_once(self):
        with db.session(self.engine) as opened:
            alert = alerts.open_alert(opened, self.settings, "imported:1", "export_imported", "jo", alerts.export_imported("3 added"), None, 0.0,
                                      repeats=alerts.ONCE, notifier=self.notifier, about=alerts.NOBODY)
            self.assertEqual(alert.closed, 0.0)
        self.assertEqual(self.tick(10 * DAY), 0)

    def test_one_shot_kinds_close_at_once_without_asking(self):
        with db.session(self.engine) as opened:
            for kind in ("export_imported", "schedule_renewed", "failure"):
                alert = alerts.open_alert(opened, self.settings, f"k:{kind}", kind, "x", alerts.failure("x"), None, 0.0, notifier=self.notifier, about=alerts.NOBODY)
                self.assertEqual(alert.closed, 0.0)
        self.assertEqual(self.tick(10 * DAY), 0)

    def test_repeats_keep_the_original_title(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.tick(DAY)
        self.assertEqual({title for title, _, _ in self.notifier.sent}, {"Google sign-in needed"})

    def test_every_send_records_an_event(self):
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
        self.tick(DAY)
        with db.session(self.engine) as opened:
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_sent", "alert_sent"])

    def test_a_failed_send_is_recorded_and_the_caller_goes_on(self):
        self.notifier = Notifier(alerts.FAILED)
        with db.session(self.engine) as opened:
            alert = self.open_sign_in(opened, 0.0)
            self.assertEqual(alert.sent_count, 1)
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_failed"])

    def test_disabled_home_assistant_is_recorded_too(self):
        self.notifier = Notifier(alerts.DISABLED)
        with db.session(self.engine) as opened:
            self.open_sign_in(opened, 0.0)
            self.assertEqual([e.type for e in events.recent(opened)], ["alert_disabled"])
            self.assertEqual(opened.execute(select(Alert)).scalars().one().sent_count, 1)

    def test_wording(self):
        self.assertIn("BACK", alerts.originals_unmatched("Jo", "BACK", 4).message.upper())
        without_pixel = alerts.originals_unmatched("Jo", None, 4).message
        self.assertIn("Jo's", without_pixel)
        self.assertNotIn("None", without_pixel)
        self.assertNotIn("delet", without_pixel.lower())
        self.assertIn("2026", alerts.schedule_ending("a@x", "2026-12-01").message)
        self.assertIn("2027", alerts.schedule_renewed("a@x", "2027-12-01").message)
        self.assertIn("2026", alerts.schedule_renewal_failed("a@x", "2026-12-01").message)
        self.assertIn("2026", alerts.schedule_renewal_unconfirmed("a@x", "2026-12-01").message)
        self.assertIn("E1", alerts.export_expiring("E1", "soon").message)
        self.assertIn("Import", alerts.failure("Import").message)


if __name__ == "__main__":
    unittest.main()


class TransportWordingTests(unittest.TestCase):
    def test_each_new_kind_has_its_signed_off_title(self):
        self.assertEqual({kind: alerts.TITLES[kind] for kind in config.SWITCHABLE_ALERTS}, {
            "syncthing_down": "Syncthing not answering", "syncthing_settings_failed": "Syncthing settings not applied",
            "syncthing_version": "Syncthing version not tested", "share_check_failed": "Photo share check failed", "pixel_held": "Pixel folder held"})

    def test_the_syncthing_texts_are_the_signed_off_ones(self):
        self.assertEqual(alerts.syncthing_down().message, "Darkroom's Syncthing has not answered for over an hour, so nothing is reaching the Pixels. Darkroom keeps restarting it, "
                         "and placing and pruning carry on; the Pixels catch up once it is back. If it stays down, restart the container.")
        self.assertEqual(alerts.syncthing_settings_failed().message, "Darkroom could not apply its settings to Syncthing or read them back, so it has resumed nothing and the "
                         "Pixels are not syncing. It tries again before the next hourly run. The Status page gives the reason.")
        self.assertEqual(alerts.syncthing_version().message, "The Syncthing in this container is not a version Darkroom was tested with, so Darkroom has not started it and "
                         "nothing reaches the Pixels. Run the published Darkroom image, which carries a tested Syncthing.")

    def test_the_share_check_text_depends_on_whose_syncthing_serves_the_pixels(self):
        self.assertEqual(alerts.share_check_failed(True).message, "The photo share did not pass Darkroom's check, so placing, pruning and syncing to the Pixels are paused, "
                         "and nothing on the share is changed. If the share is not mounted, fix the mapping and restart the container. If it really was replaced, bind the new "
                         "one as the runbook says. The Status page gives the reason.")
        self.assertEqual(alerts.share_check_failed(False).message, "The photo share did not pass Darkroom's check, so placing and pruning are paused, and nothing on the share "
                         "is changed. Pause the Syncthing that serves the Pixels until it passes again. If the share is not mounted, fix the mapping and restart the container. "
                         "If it really was replaced, bind the new one as the runbook says.")

    def test_a_held_pixel_is_named_and_counted_in_the_singular_at_one(self):
        self.assertEqual(alerts.pixel_held("Pixel 5", 3, 0).message, "3 photos could not be written to Pixel 5's folder again, so Darkroom is not sharing the folder with it. "
                         "Nothing is deleted from the Pixel. Something else is in their place, or Darkroom can't read it; the runbook says what to look for.")
        self.assertEqual(alerts.pixel_held("Pixel 5", 0, 1).message, "Pixel 5's folder has 1 file Darkroom did not put there, so Darkroom is not sharing the folder with it. "
                         "Darkroom never removes or moves them: remove them, or move them out of the folder, by hand.")
        both = alerts.pixel_held("Pixel 5", 1, 2).message
        self.assertTrue(both.startswith("1 photo could not be written to Pixel 5's folder again"))
        self.assertTrue(both.endswith("the runbook says what to look for. It also has 2 files Darkroom did not put there."))

    def test_alerts_off_takes_only_the_new_kinds(self):
        base = config.load()
        self.assertEqual(config.alerts_off_problems(dataclasses.replace(base, alerts_off=("pixel_held", "syncthing_down"))), [])
        self.assertEqual(config.alerts_off_problems(dataclasses.replace(base, alerts_off=("sign_in_needed", "nope"))),
                         ["DARKROOM_ALERTS_OFF names 2 unknown alert kinds; it takes only syncthing_down, syncthing_settings_failed, syncthing_version, share_check_failed, pixel_held"])
        with mock.patch.dict(os.environ, {"DARKROOM_ALERTS_OFF": "pixel_held, syncthing_down"}):
            self.assertEqual(config.load().alerts_off, ("pixel_held", "syncthing_down"))


DEVICE_CANARY = "CANARY7-DEVICEI-DXXXXXX-YYYYYYY-ZZZZZZZ-AAAAAAA-BBBBBBB-CCCCCCC"
FILE_CANARY = "canary-held-file-3f9c2b71"
START = test_pixel_folder.NOW


class TransportAlertTests(unittest.TestCase):
    setUp_folders = test_pixel_folder.PixelFolderTests.setUp
    account = test_pixel_folder.PixelFolderTests.account
    item = test_pixel_folder.PixelFolderTests.item
    match = test_pixel_folder.PixelFolderTests.match
    tend = test_pixel_folder.PixelFolderTests.tend
    copy_path = test_pixel_folder.PixelFolderTests.copy_path

    def setUp(self):
        self.setUp_folders()
        self.settings = dataclasses.replace(config.load(), syncthing=True, public_url="https://d.example")
        self.notifier = Notifier()
        jo_pixel = self.opened.get(Pixel, self.jo_pixel)
        jo_pixel.name, jo_pixel.syncthing_device_id = "Pixel 5", DEVICE_CANARY
        self.opened.get(Pixel, self.sam_pixel).name = "Pixel 8"
        self.opened.commit()

    def event(self, kind, subject, detail=None, at=START):
        events.record(self.opened, at, kind, subject, detail)
        self.opened.commit()

    def row(self, pixel_id, **values):
        row = self.opened.get(PixelTransport, pixel_id) or PixelTransport(pixel_id=pixel_id, updated=START)
        self.opened.add(row)
        for name, value in values.items():
            setattr(row, name, value)
        self.opened.commit()

    def review(self, at, settings=None):
        transport_status.review(self.opened, settings or self.settings, at, self.notifier)

    def open_kinds(self):
        return sorted((alert.kind, alert.subject) for alert in self.opened.execute(select(Alert).where(Alert.closed.is_(None))).scalars())

    def test_nothing_opens_while_syncthing_runs_and_every_pixel_is_fine(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.row(self.jo_pixel, connected=True, last_seen=START, completion=100.0)
        self.review(START + DAY)
        self.assertEqual(self.open_kinds(), [])

    def test_syncthing_not_answering_alerts_after_an_hour_and_closes_once_it_answers(self):
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE)
        self.review(START + HOUR - 1)
        self.assertEqual(self.open_kinds(), [])
        self.review(START + HOUR)
        self.assertEqual(self.open_kinds(), [("syncthing_down", "syncthing")])
        alert = self.opened.execute(select(Alert)).scalar_one()
        self.assertEqual((alert.key, alert.link, alert.person_id, alert.account_id), (transport_status.DOWN_KEY, "https://d.example/", None, None))
        self.assertEqual(self.notifier.sent, [("Syncthing not answering", alerts.syncthing_down().message, "https://d.example/")])
        self.event(transport.RECOVERED, transport.SUBJECT, at=START + 2 * HOUR)
        self.review(START + 2 * HOUR)
        self.assertEqual(self.open_kinds(), [])

    def test_a_restart_that_recovers_within_the_hour_never_alerts(self):
        self.event(syncthing_version.EVENT, syncthing_version.SUBJECT, syncthing_version.NOT_ANSWERING)
        self.review(START + HOUR - 60)
        self.event(transport.RECOVERED, transport.SUBJECT, at=START + HOUR - 30)
        self.review(START + 2 * HOUR)
        self.assertEqual(self.open_kinds(), [])
        self.assertEqual(self.notifier.sent, [])

    def test_a_key_not_handed_over_or_refused_at_start_is_syncthing_down_at_once(self):
        for reason in (syncthing_version.NO_KEY, syncthing_version.KEY_REFUSED):
            with self.subTest(reason=reason):
                self.event(syncthing_version.EVENT, syncthing_version.SUBJECT, reason)
                self.review(START + 1)
                self.assertEqual(self.open_kinds(), [("syncthing_down", "syncthing")])
                self.event(transport.RECOVERED, transport.SUBJECT)
                self.review(START + 2)
                self.assertEqual(self.open_kinds(), [])

    def test_settings_that_cannot_be_applied_or_read_back_alert_at_once(self):
        for reason in (transport.NOT_KEPT, transport.KEPT_NOT_SEND_ONLY, transport.REFUSED_KEY):
            with self.subTest(reason=reason):
                self.event(transport.FAILED, transport.SUBJECT, reason)
                self.review(START + 1)
                self.assertEqual(self.open_kinds(), [("syncthing_settings_failed", "syncthing")])
                self.event(transport.RECOVERED, transport.SUBJECT)
                self.review(START + 2)
                self.assertEqual(self.open_kinds(), [])

    def test_a_version_out_of_range_alerts_and_a_tested_one_closes_it(self):
        self.event(syncthing_version.EVENT, syncthing_version.SUBJECT, "Syncthing reported v2.2.0, outside the tested range v2.1.7")
        self.review(START + 1)
        self.assertEqual(self.open_kinds(), [("syncthing_version", "syncthing")])
        self.assertEqual(self.notifier.sent[0][:2], ("Syncthing version not tested", alerts.syncthing_version().message))
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.review(START + 2)
        self.assertEqual(self.open_kinds(), [])

    def test_a_failed_share_check_alerts_on_the_first_check_and_a_passing_one_closes_it(self):
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.MISSING)
        self.review(START)
        self.assertEqual(self.open_kinds(), [("share_check_failed", "share")])
        self.assertEqual(self.notifier.sent, [("Photo share check failed", alerts.share_check_failed(True).message, "https://d.example/")])
        self.event(share_check.PASSED, share_check.SUBJECT)
        self.review(START + 1)
        self.assertEqual(self.open_kinds(), [])

    def test_with_a_syncthing_set_up_by_hand_only_the_share_check_alerts_in_its_own_words(self):
        by_hand = dataclasses.replace(self.settings, syncthing=False)
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.CHANGED)
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE, at=START - DAY)
        self.event(syncthing_version.EVENT, syncthing_version.SUBJECT, "Syncthing reported v2.2.0, outside the tested range v2.1.7")
        self.review(START + DAY, by_hand)
        self.assertEqual(self.open_kinds(), [("share_check_failed", "share")])
        self.assertEqual(self.notifier.sent[0][1], alerts.share_check_failed(False).message)

    def test_a_share_that_recovers_closes_its_alert_without_clearing_the_newer_syncthing_warning(self):
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE)
        self.review(START + 2 * HOUR)
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.MISSING, at=START + 3 * HOUR)
        self.event(transport.PAUSED, transport.SUBJECT, share_check.MISSING, at=START + 3 * HOUR)
        self.review(START + 3 * HOUR)
        self.assertEqual(self.open_kinds(), [("share_check_failed", "share"), ("syncthing_down", "syncthing")])
        self.event(share_check.PASSED, share_check.SUBJECT, at=START + 4 * HOUR)
        self.review(START + 4 * HOUR)
        self.assertEqual(self.open_kinds(), [("syncthing_down", "syncthing")])
        self.event(transport.RECOVERED, transport.SUBJECT, at=START + 5 * HOUR)
        self.review(START + 5 * HOUR)
        self.assertEqual(self.open_kinds(), [])

    def held_by_strays(self, count=2):
        folder = self.photos.pixel_root(self.jo_pixel)
        (folder / "jo").mkdir(parents=True, exist_ok=True)
        for number in range(count):
            (folder / "jo" / f"{FILE_CANARY}-{number}.HEIC").write_bytes(b"made up")
        pixel_check.check(self.opened, self.photos, START)
        self.row(self.jo_pixel, error=transport.HELD)

    def test_a_pixel_held_for_unaccounted_files_is_named_and_counted_and_carries_no_file_name_or_device_id(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        with no_leaks(self, DEVICE_CANARY, FILE_CANARY, replies=self.notifier.texts) as canaries:
            self.held_by_strays()
            self.review(START + 1)
            alert = self.opened.execute(select(Alert)).scalar_one()
            self.notifier.texts += [f"{alert.key} {alert.subject} {alert.message} {alert.link}".encode()]
            canaries.add(f"{FILE_CANARY}-0")
        self.assertEqual((alert.kind, alert.key, alert.subject, alert.link), ("pixel_held", f"syncthing:held:{self.jo_pixel}", f"pixel {self.jo_pixel}",
                                                                             "https://d.example/people"))
        self.assertEqual(alert.message, alerts.pixel_held("Pixel 5", 0, 2).message)
        self.row(self.jo_pixel, error=None)
        self.review(START + 2)
        self.assertEqual(self.open_kinds(), [])

    def test_a_pixel_whose_photos_cannot_be_written_again_is_held_by_count(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.opened.add(PixelCopy(pixel_id=self.jo_pixel, person_id=self.jo, item_id=self.item(self.jo).id, sha1="0" * 40, bytes=1, state="queued", queued_at=START,
                                  placed_at=START - DAY, name="x.jpg"))
        self.opened.commit()
        self.row(self.jo_pixel, error=transport.HELD)
        self.review(START + 1)
        self.assertEqual(self.opened.execute(select(Alert.message)).scalar_one(), alerts.pixel_held("Pixel 5", 1, 0).message)

    def test_a_pixel_still_being_checked_or_not_seen_for_days_is_shown_and_never_alerted(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.row(self.jo_pixel, error=transport.CHECKING)
        self.opened.get(Pixel, self.sam_pixel).syncthing_device_id = "MADEUP2-DEVICE"
        self.row(self.sam_pixel, connected=False, last_seen=START - 10 * DAY)
        self.review(START)
        self.assertEqual(self.open_kinds(), [])
        seen = transport_status.overview(self.opened, START)
        self.assertEqual((seen.pixels[self.jo_pixel].state, seen.pixels[self.sam_pixel].state, seen.pixels[self.sam_pixel].days), ("checking", "not_seen", 10))

    def test_a_held_alert_is_left_as_it_is_while_syncthing_is_not_running(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.held_by_strays()
        self.review(START + 1)
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.MISSING)
        self.event(transport.PAUSED, transport.SUBJECT, share_check.MISSING)
        self.row(self.jo_pixel, error=transport.SHARE)
        self.review(START + 2)
        self.assertEqual(self.open_kinds(), [("pixel_held", f"pixel {self.jo_pixel}"), ("share_check_failed", "share")])

    def test_a_kind_switched_off_closes_even_while_the_share_check_fails_or_syncthing_is_not_running(self):
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE)
        self.review(START + HOUR)
        self.event(transport.RECOVERED, transport.SUBJECT, at=START + HOUR + 1)
        self.held_by_strays()
        self.review(START + HOUR + 2)
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE, at=START + HOUR + 3)
        self.review(START + 3 * HOUR)
        self.assertEqual(self.open_kinds(), [("pixel_held", f"pixel {self.jo_pixel}"), ("syncthing_down", "syncthing")])
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.MISSING, at=START + 3 * HOUR + 1)
        self.event(transport.PAUSED, transport.SUBJECT, share_check.MISSING, at=START + 3 * HOUR + 1)
        self.review(START + 3 * HOUR + 2, dataclasses.replace(self.settings, alerts_off=("syncthing_down",)))
        self.assertEqual(self.open_kinds(), [("pixel_held", f"pixel {self.jo_pixel}"), ("share_check_failed", "share")])
        self.review(START + 3 * HOUR + 3, dataclasses.replace(self.settings, alerts_off=("syncthing_down", "pixel_held")))
        self.assertEqual(self.open_kinds(), [("share_check_failed", "share")])

    def test_a_switched_off_held_alert_closes_while_syncthing_is_down(self):
        self.event(transport.RECOVERED, transport.SUBJECT)
        self.held_by_strays()
        self.review(START + 1)
        self.event(transport.FAILED, transport.SUBJECT, transport.UNREACHABLE, at=START + 2)
        self.review(START + 3)
        self.assertEqual(self.open_kinds(), [("pixel_held", f"pixel {self.jo_pixel}")])
        self.review(START + 4, dataclasses.replace(self.settings, alerts_off=("pixel_held",)))
        self.assertEqual(self.open_kinds(), [])

    def test_a_kind_switched_off_never_opens_and_an_open_one_closes(self):
        self.event(share_check.FAILED, share_check.SUBJECT, share_check.MISSING)
        self.review(START)
        self.assertEqual(self.open_kinds(), [("share_check_failed", "share")])
        self.review(START + 1, dataclasses.replace(self.settings, alerts_off=("share_check_failed",)))
        self.assertEqual(self.open_kinds(), [])
        self.event(transport.FAILED, transport.SUBJECT, transport.NOT_KEPT)
        self.event(share_check.PASSED, share_check.SUBJECT)
        self.review(START + 2, dataclasses.replace(self.settings, alerts_off=("syncthing_settings_failed",)))
        self.assertEqual(self.open_kinds(), [])
        self.assertEqual(len(self.notifier.sent), 1)
