import unittest
from unittest import mock

from sqlalchemy import select

from darkroom import tls
from darkroom.domain.ingest import run
from darkroom.providers import immich
from darkroom.storage.models import Alert, Event, Export, Person
from tests.fakes.immich import FakeImmich, FakePerson
from tests.import_world import ADMIN_KEY, NOW, World
from tests.privacy import no_leaks
from tests.shared import settings_from

PEOPLE = [FakePerson("jo", "u-jo", "lib-jo", "key-jo"), FakePerson("sam", "u-sam", "lib-sam", "key-sam"),
          FakePerson("lee", "u-lee", "lib-lee-1", "key-lee"), FakePerson("lee2", "u-lee", "lib-lee-2", "key-lee2")]


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeImmich(PEOPLE, ADMIN_KEY).start()
        self.addCleanup(self.fake.stop)
        self.client = immich.Client(self.fake.url, ADMIN_KEY, timeout=5.0)

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_lists_every_library_with_its_owner(self):
        self.assertEqual(self.client.list_libraries()[:2], [immich.Library("lib-jo", "u-jo"), immich.Library("lib-sam", "u-sam")])

    def test_finds_the_library_owned_by_a_user(self):
        self.assertEqual(self.client.find_library("u-sam"), "lib-sam")
        self.assertIsNone(self.client.find_library("u-nobody"))

    def test_a_user_with_several_libraries_is_refused(self):
        with self.assertRaisesRegex(immich.ImmichFailed, "several libraries"):
            self.client.find_library("u-lee")

    def test_rescan_is_sent_with_the_admin_key(self):
        self.client.rescan_library("lib-jo")
        self.assertEqual([(scan.library_id, scan.key_role) for scan in self.fake.scans()], [("lib-jo", "admin")])

    def test_an_unknown_library_is_a_404(self):
        with self.assertRaises(immich.ImmichFailed) as caught:
            self.client.rescan_library("lib-gone")
        self.assertEqual(caught.exception.status, 404)

    def test_a_library_id_with_a_slash_is_refused_before_any_request(self):
        with self.assertRaises(immich.ImmichFailed):
            self.client.rescan_library("../users")
        self.assertEqual(self.fake.scans(), [])

    def test_a_wrong_key_is_refused(self):
        with self.assertRaises(immich.ImmichFailed) as caught:
            immich.Client(self.fake.url, "not-the-key").rescan_library("lib-jo")
        self.assertEqual(caught.exception.status, 401)

    def test_a_person_key_cannot_scan(self):
        with self.assertRaisesRegex(immich.ImmichFailed, "403"):
            immich.Client(self.fake.url, "key-jo").rescan_library("lib-jo")

    def test_each_failure_mode_raises_without_the_key_in_the_message(self):
        for mode, wanted in (("401", "refused the admin key"), ("500", "answered 500"), ("down", "could not be reached")):
            with self.subTest(mode=mode):
                self.fake.mode(mode)
                for call in (self.client.ping, self.client.list_libraries, lambda: self.client.rescan_library("lib-jo")):
                    with self.assertRaises(immich.ImmichFailed) as caught:
                        call()
                    self.assertIn(wanted, str(caught.exception))
                    self.assertNotIn(ADMIN_KEY, str(caught.exception))
        self.assertEqual(self.fake.scans(), [])

    def test_a_reply_that_is_not_json_or_not_a_list_is_refused(self):
        with mock.patch.object(immich.Client, "call", return_value={"not": "a list"}), self.assertRaisesRegex(immich.ImmichFailed, "not a list"):
            self.client.list_libraries()
        with mock.patch.object(tls, "urlopen") as opened:
            opened.return_value.__enter__.return_value.read.return_value = b"<html>"
            with self.assertRaisesRegex(immich.ImmichFailed, "not JSON"):
                self.client.list_libraries()
            opened.return_value.__enter__.return_value.read.return_value = b"x" * (immich.MAX_REPLY + 1)
            with self.assertRaisesRegex(immich.ImmichFailed, "too large"):
                self.client.list_libraries()

    def test_plain_http_is_allowed_only_on_the_lan(self):
        with self.assertRaisesRegex(immich.ImmichFailed, "only https"):
            immich.Client("http://immich.example.com", ADMIN_KEY).ping()

    def test_settings_without_url_or_key_are_not_configured(self):
        with self.assertRaises(immich.NotConfigured):
            immich.Client.of(settings_from(IMMICH_URL=self.fake.url))
        with self.assertRaises(immich.NotConfigured):
            immich.Client.of(settings_from(IMMICH_ADMIN_KEY=ADMIN_KEY))
        self.assertEqual(immich.Client.of(settings_from(IMMICH_URL=self.fake.url, IMMICH_ADMIN_KEY=ADMIN_KEY)).url, self.fake.url)


class RescanTests(unittest.TestCase):
    def setUp(self):
        self.world = World(self)
        self.client = immich.Client(self.world.fake.url, ADMIN_KEY, timeout=5.0)
        self.export = self.world.opened.get(Export, self.world.hand_dropped().id)
        self.jo = self.world.opened.get(Person, self.world.ids["jo"])

    def rescan(self, *slugs):
        return run.rescan_people(self.world.opened, self.world.settings, [self.world.ids[slug] for slug in slugs], self.export, NOW,
                                 run.Tools(immich=self.client, notifier=self.world.recorder))

    def failures(self):
        return [(e.subject, e.detail) for e in self.world.opened.execute(select(Event).where(Event.type == "immich_failed")).scalars()]

    def test_the_library_is_found_by_its_owner_and_cached_on_the_person(self):
        self.assertEqual(self.rescan("jo"), 1)
        self.assertEqual(self.jo.immich_library_id, "lib-jo")
        with mock.patch.object(immich.Client, "list_libraries", side_effect=AssertionError("cached")):
            self.assertEqual(self.rescan("jo"), 1)
        self.assertEqual(self.world.scanned(), ["lib-jo", "lib-jo"])

    def test_a_cached_library_that_is_gone_is_forgotten_and_reported(self):
        self.jo.immich_library_id = "lib-gone"
        self.assertEqual(self.rescan("jo"), 0)
        self.assertIsNone(self.jo.immich_library_id)
        self.assertEqual(self.failures(), [("jo", "Immich answered 404")])
        self.assertEqual(self.rescan("jo"), 1)

    def test_a_person_without_an_immich_user_is_reported(self):
        self.jo.immich_user_id = None
        self.assertEqual(self.rescan("jo", "sam"), 1)
        self.assertEqual(self.failures(), [("jo", "the person has no Immich user")])
        self.assertEqual(self.world.scanned(), ["lib-sam"])

    def test_a_user_without_a_library_is_reported(self):
        self.jo.immich_user_id = "u-nobody"
        self.assertEqual(self.rescan("jo"), 0)
        self.assertEqual(self.failures(), [("jo", "the person's Immich user owns no library")])

    def test_each_error_mode_becomes_an_event_and_a_failure_alert(self):
        for mode in ("down", "401", "500"):
            with self.subTest(mode=mode):
                self.world.fake.mode(mode)
                self.assertEqual(self.rescan("jo"), 0)
        self.assertEqual([subject for subject, _ in self.failures()], ["jo", "jo", "jo"])
        failure = self.world.opened.execute(select(Alert).where(Alert.kind == "failure")).scalars().first()
        self.assertEqual(failure.message, "The Immich rescan of Jo's library failed. Open Darkroom for details.")
        self.assertEqual(self.world.scanned(), [])

    def test_the_admin_key_never_reaches_events_logs_or_the_database(self):
        self.world.fake.mode("401")
        with no_leaks(self, ADMIN_KEY, database=self.world.engine):
            self.rescan("jo", "sam")

    def test_an_import_whose_rescan_fails_is_still_imported_with_an_alert(self):
        self.world.fake.mode("500")
        done = self.world.run_import(self.export)
        self.assertEqual(done.status, "done")
        self.assertEqual(self.world.opened.get(Export, self.export.id).status, "imported")
        self.assertEqual(self.world.sent("Darkroom needs a look"), 2)
        self.assertEqual(self.world.sent("Takeout import finished"), 1)

    def test_no_change_means_no_rescan(self):
        self.assertEqual(self.rescan(), 0)
        self.assertEqual(self.world.scanned(), [])


if __name__ == "__main__":
    unittest.main()
