import tempfile
from pathlib import Path
import unittest

from darkroom.domain import accounts, browser_jobs
from darkroom.storage import db
from darkroom.storage.models import BrowserJob, GoogleAccount, Pixel, PixelPerson
from darkroom.storage.paths import Photos
from tests.shared import add_person, own_database


def add_account(opened, email, default_person):
    account = GoogleAccount(email=email, default_person_id=default_person, created=1.0)
    opened.add(account)
    opened.flush()
    return account.id


def add_pixel(opened, account_id, people):
    pixel = Pixel(name="Pixel", google_account_id=account_id, created=1.0)
    opened.add(pixel)
    opened.flush()
    for person in people:
        opened.add(PixelPerson(pixel_id=pixel.id, person_id=person))
    opened.flush()
    return pixel.id


class ServedPeopleTests(unittest.TestCase):
    def test_an_account_with_no_pixels_serves_its_default_person(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo = add_person(opened, "jo")
            add_person(opened, "sam")
            account = add_account(opened, "jo@x.test", jo)
            self.assertEqual(accounts.served_people(opened, account), {jo})

    def test_a_shared_pixel_adds_its_people(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam, kim = (add_person(opened, slug) for slug in ("jo", "sam", "kim"))
            account = add_account(opened, "family@x.test", jo)
            add_pixel(opened, account, [sam, kim])
            self.assertEqual(accounts.served_people(opened, account), {jo, sam, kim})

    def test_one_person_per_pixel_is_the_plain_case(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo = add_person(opened, "jo")
            account = add_account(opened, "jo@x.test", jo)
            add_pixel(opened, account, [jo])
            self.assertEqual(accounts.served_people(opened, account), {jo})

    def test_accounts_do_not_leak_into_each_other(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam = add_person(opened, "jo"), add_person(opened, "sam")
            a, b = add_account(opened, "a@x.test", jo), add_account(opened, "b@x.test", sam)
            add_pixel(opened, a, [jo])
            self.assertEqual(accounts.served_people(opened, a), {jo})
            self.assertEqual(accounts.served_people(opened, b), {sam})

    def test_an_unknown_account_serves_nobody(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            self.assertEqual(accounts.served_people(opened, 999), set())

    def test_accounts_serving_a_person(self):
        engine = own_database(self)
        with db.session(engine) as opened:
            jo, sam = add_person(opened, "jo"), add_person(opened, "sam")
            own = add_account(opened, "jo@x.test", jo)
            shared = add_account(opened, "family@x.test", sam)
            add_pixel(opened, shared, [jo])
            self.assertEqual(accounts.accounts_serving(opened, jo), {own, shared})
            self.assertEqual(accounts.accounts_serving(opened, sam), {shared})


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.engine = own_database(self)
        self.photos = Photos(Path(self.enterContext(tempfile.TemporaryDirectory())))

    def account_with_job(self, status):
        with db.session(self.engine) as opened:
            name = f"jo{status.replace('_', '')}"
            account = add_account(opened, f"{name}@x.test", add_person(opened, name))
            job = browser_jobs.enqueue(opened, account, "noop", None, 1.0)
            job.status = status
        return account

    def test_every_unfinished_job_status_refuses_the_delete_and_changes_nothing(self):
        for status in browser_jobs.UNFINISHED_STATUSES:
            with self.subTest(status=status):
                account = self.account_with_job(status)
                with db.session(self.engine) as opened:
                    with self.assertRaisesRegex(accounts.InUse, "browser job that has not finished"):
                        accounts.delete(opened, self.photos, account, 2.0)
                with db.session(self.engine) as opened:
                    self.assertIsNotNone(opened.get(GoogleAccount, account))
                    self.assertEqual(opened.query(BrowserJob).filter_by(account_id=account).count(), 1)

    def test_finished_jobs_do_not_block_the_delete(self):
        for status in ("done", "failed"):
            with self.subTest(status=status):
                account = self.account_with_job(status)
                with db.session(self.engine) as opened:
                    accounts.delete(opened, self.photos, account, 2.0)
                with db.session(self.engine) as opened:
                    self.assertIsNone(opened.get(GoogleAccount, account))


if __name__ == "__main__":
    unittest.main()
