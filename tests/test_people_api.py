import contextlib
import errno
import io
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from sqlalchemy import select

from darkroom.storage import db, secretbox
from darkroom.storage.models import Event, ImmichAsset, MediaItem, Person
from tests.api_world import SECRET_KEY, ApiCase
from tests.privacy import no_leaks

IMMICH_KEY = "immich-api-key-canary-0123456789abcdef"


class PeopleApiTests(ApiCase):
    def test_a_new_person_gets_a_slug_and_its_folders(self):
        person = self.person("Jo Example")
        self.assertEqual((person["slug"], person["name"], person["has_immich_key"]), ("jo-example", "Jo Example", False))
        for folder in ("originals", "google-only"):
            self.assertTrue((self.photos / "jo-example" / folder).is_dir())
        self.assertTrue((self.photos / "_inbox" / "jo-example" / "manual").is_dir())
        status, listed = self.call("GET", "/api/people")
        self.assertEqual((status, [p["id"] for p in listed["people"]]), (200, [person["id"]]))

    def test_a_chosen_slug_is_used_and_must_be_a_folder_name(self):
        self.assertEqual(self.person("Sam", slug="sam")["slug"], "sam")
        for slug in ("Sam", "../x", "-a", "a b"):
            with self.subTest(slug=slug):
                status, reply = self.call("POST", "/api/people", {"name": "Other", "slug": slug})
                self.assertEqual(status, 400)
                self.assertIn("slug", reply["error"])
        status, reply = self.call("POST", "/api/people", {"name": "!!!"})
        self.assertEqual(status, 400)

    def test_a_taken_slug_is_a_conflict_and_creates_nothing(self):
        self.person("Sam", slug="sam")
        status, reply = self.call("POST", "/api/people", {"name": "Other Sam", "slug": "sam"})
        self.assertEqual(status, 409)
        self.assertIn("sam", reply["error"])
        with db.session(self.engine) as opened:
            self.assertEqual(len(opened.execute(select(Person)).scalars().all()), 1)

    def test_validation_errors_are_400s_with_a_message(self):
        cases = (({}, "name is required"), ({"name": "  "}, "name is required"), ({"name": 5}, "name must be text"), ({"name": "x" * 101}, "name is too long"),
                 ({"name": "Jo", "email": "nope"}, "email must be an email address"), ({"name": "Jo", "immich_user_id": 4}, "immich_user_id must be text"))
        for body, message in cases:
            with self.subTest(body=body):
                self.assertEqual(self.call("POST", "/api/people", body), (400, {"error": message}))
        self.assertEqual(self.call("POST", "/api/people", raw=b"not json"), (400, {"error": "the request is not valid JSON"}))
        self.assertEqual(self.call("POST", "/api/people", raw=b"[]"), (400, {"error": "the request must be a JSON object"}))

    def test_a_new_immich_user_restarts_the_reconcile_window_and_drops_unfinished_rows_only(self):
        person = self.person("Jo", immich_user_id="u-1")
        with db.session(self.engine) as opened:
            opened.get(Person, person["id"]).immich_reconciled_until = 99.0
            for name, status in (("a", "stored"), ("b", "waiting"), ("c", "failed"), ("d", "ignored")):
                opened.add(ImmichAsset(person_id=person["id"], asset_id=name, kind="photo", status=status, sha1="ab", seen_at=1.0))
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"name": "Jo", "immich_user_id": "u-1"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertEqual(opened.get(Person, person["id"]).immich_reconciled_until, 99.0)
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"immich_user_id": "u-2"})[0], 200)
        with db.session(self.engine) as opened:
            self.assertIsNone(opened.get(Person, person["id"]).immich_reconciled_until)
            self.assertEqual([row.asset_id for row in opened.execute(select(ImmichAsset)).scalars()], ["a"])

    def test_update_changes_only_what_is_sent_and_null_clears(self):
        person = self.person("Jo", email="jo@example.test", immich_user_id="u-1", immich_library_id="l-1")
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"name": "Joanna", "immich_user_id": None})
        self.assertEqual(status, 200)
        self.assertEqual((reply["person"]["name"], reply["person"]["immich_user_id"], reply["person"]["email"], reply["person"]["immich_library_id"]),
                         ("Joanna", None, "jo@example.test", "l-1"))
        self.assertEqual(reply["person"]["slug"], "jo")
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"email": None})
        self.assertEqual(reply["person"]["email"], None)
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {})[0], 200)

    def test_update_validation_and_404s(self):
        person = self.person("Jo")
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"name": ""}), (400, {"error": "name is required"}))
        self.assertEqual(self.call("PATCH", f"/api/people/{person['id']}", {"email": "bad"})[0], 400)
        self.assertEqual(self.call("PATCH", "/api/people/999", {"name": "X"}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("PATCH", "/api/people/abc", {"name": "X"}), (404, {"error": "no such person"}))
        self.assertEqual(self.call("DELETE", "/api/people/999"), (404, {"error": "no such person"}))
        self.assertEqual(self.call("DELETE", "/api/people/abc"), (404, {"error": "no such person"}))

    def test_the_immich_key_is_sealed_and_never_returned_logged_or_stored_plain(self):
        with no_leaks(self, IMMICH_KEY, database=self.engine, replies=self.replies):
            person = self.person("Jo", immich_api_key=IMMICH_KEY)
            self.assertTrue(person["has_immich_key"])
            self.assertNotIn("immich_api_key", person)
            reply = self.call("PATCH", f"/api/people/{person['id']}", {"name": "Joanna"})[1]
            self.assertTrue(reply["person"]["has_immich_key"])
            listed = self.call("GET", "/api/people")[1]
            self.assertTrue(listed["people"][0]["has_immich_key"])
            with db.session(self.engine) as opened:
                sealed = opened.get(Person, person["id"]).immich_api_key_sealed
                self.assertTrue(secretbox.is_sealed(sealed))
                self.assertEqual(secretbox.unseal(sealed, SECRET_KEY), IMMICH_KEY)
                events = [(e.type, e.subject, e.detail) for e in opened.execute(select(Event)).scalars()]
            self.assertIn(("immich_key_set", "jo", None), events)
            self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": IMMICH_KEY + "-2"})
            self.assertEqual(self.call("GET", "/api/events")[0], 200)

    def test_a_rejected_key_is_not_echoed(self):
        with no_leaks(self, IMMICH_KEY * 20, database=self.engine, replies=self.replies):
            status, reply = self.call("POST", "/api/people", {"name": "Jo", "immich_api_key": IMMICH_KEY * 20})
            self.assertEqual(status, 400)
            self.assertEqual(reply, {"error": "immich_api_key is too long"})
            self.assertEqual(self.call("POST", "/api/people", {"name": "Jo", "immich_api_key": 12345678})[0], 400)

    def test_null_removes_the_key_and_the_event_says_so(self):
        person = self.person("Jo", immich_api_key=IMMICH_KEY)
        status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": None})
        self.assertEqual((status, reply["person"]["has_immich_key"]), (200, False))
        self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": None})
        with db.session(self.engine) as opened:
            types = [e.type for e in opened.execute(select(Event).order_by(Event.id)).scalars()]
        self.assertEqual(types.count("immich_key_removed"), 1)

    def test_deleting_is_refused_while_the_person_has_media_accounts_or_pixels_and_files_stay(self):
        person = self.person("Jo")
        pid = person["id"]
        with db.session(self.engine) as opened:
            opened.add(MediaItem(person_id=pid, source="phone", path="jo/originals/a.jpg", sha1="a" * 40, bytes=1, indexed_at=1.0))
        status, reply = self.call("DELETE", f"/api/people/{pid}")
        self.assertEqual(status, 409)
        self.assertIn("media items", reply["error"])
        with db.session(self.engine) as opened:
            opened.query(MediaItem).delete()
        self.account(pid)
        self.assertIn("Google accounts", self.call("DELETE", f"/api/people/{pid}")[1]["error"])
        self.assertEqual(self.call("GET", "/api/people")[1]["people"][0]["id"], pid)

    def test_deleting_an_unused_person_removes_the_record_but_never_the_folders(self):
        person = self.person("Jo")
        original = self.photos / "jo" / "originals" / "keep.jpg"
        original.write_bytes(b"photo bytes")
        other = self.person("Sam")
        account = self.account(other["id"])
        pixel = self.pixel(account["id"], people_ids=[other["id"], person["id"]])
        self.assertIn("Pixels", self.call("DELETE", f"/api/people/{person['id']}")[1]["error"])
        self.call("PATCH", f"/api/pixels/{pixel['id']}", {"people_ids": [other["id"]]})
        self.assertEqual(self.call("DELETE", f"/api/people/{person['id']}"), (200, {"ok": True}))
        self.assertEqual([p["slug"] for p in self.call("GET", "/api/people")[1]["people"]], ["sam"])
        self.assertEqual(original.read_bytes(), b"photo bytes")

    def test_a_deleted_persons_folder_name_is_never_reused_and_its_files_are_not_indexed(self):
        old = self.person("Jo")
        original = self.photos / "jo" / "originals" / "keep.jpg"
        original.write_bytes(b"photo bytes")
        self.assertEqual(self.call("DELETE", f"/api/people/{old['id']}"), (200, {"ok": True}))
        status, reply = self.call("POST", "/api/people", {"name": "Someone Else", "slug": "jo"})
        self.assertEqual(status, 409)
        self.assertIn("already in use on disk", reply["error"])
        self.assertIn("never reused or deleted automatically", reply["error"])
        self.assertEqual(self.call("POST", "/api/people", {"name": "Jo"})[0], 409)
        self.assertEqual(original.read_bytes(), b"photo bytes")
        self.assertEqual(self.call("GET", "/api/people"), (200, {"people": []}))
        with db.session(self.engine) as opened:
            self.assertEqual(opened.execute(select(MediaItem)).scalars().all(), [])
        self.assertEqual(self.person("Kai")["slug"], "kai")

    def test_a_leftover_inbox_folder_also_blocks_the_name(self):
        (self.photos / "_inbox" / "kai").mkdir(parents=True)
        self.assertEqual(self.call("POST", "/api/people", {"name": "Kai"})[0], 409)
        self.assertFalse((self.photos / "kai").exists())

    def test_a_folder_that_cannot_be_made_is_a_clear_500_and_no_person_and_a_retry_works(self):
        (self.photos / "_inbox").write_bytes(b"in the way")
        before = sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*"))
        status, reply = self.call("POST", "/api/people", {"name": "Jo"})
        self.assertEqual(status, 500)
        self.assertEqual(reply, {"error": "Darkroom could not create the person's folders: see the Darkroom server log for the cause"})
        self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})
        self.assertEqual(sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*")), before)
        self.assertFalse((self.photos / "jo").exists())
        (self.photos / "_inbox").unlink()
        self.assertEqual(self.call("POST", "/api/people", {"name": "Jo"})[0], 201)
        self.assertTrue((self.photos / "jo" / "originals").is_dir())


class FolderFailureTests(ApiCase):
    NAME = "Canary Person Name"

    def fail_with(self, code):
        real = Path.mkdir

        def failing(path, *args, **kwargs):
            if path.name == "manual":
                raise OSError(code, os.strerror(code), str(path.parent.parent))
            return real(path, *args, **kwargs)

        before = sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*"))
        with mock.patch.object(Path, "mkdir", failing), no_leaks(self, self.NAME, str(self.photos), database=self.engine, replies=self.replies) as watched:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status, reply = self.call("POST", "/api/people", {"name": self.NAME, "slug": "cpn1"})
        self.assertEqual(sorted(str(p.relative_to(self.photos)) for p in self.photos.rglob("*")), before)
        self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})
        self.assertTrue(watched)
        return status, reply, err.getvalue()

    @staticmethod
    def folder_line(logged):
        lines = [json.loads(text) for text in logged.strip().splitlines()]
        return next(line for line in lines if line["event"] == "person folders could not be created")

    def check(self, code, name, cause):
        status, reply, logged = self.fail_with(code)
        self.assertEqual((status, reply), (500, {"error": f"Darkroom could not create the person's folders: {cause}"}))
        line = self.folder_line(logged)
        self.assertEqual(line, {"event": "person folders could not be created", "operation": "create person", "errno": name, "folder": "_inbox"})
        for private in (self.NAME, str(self.photos), "cpn1"):
            self.assertNotIn(private, logged)
            self.assertNotIn(private, json.dumps(reply))

    def test_permission_denied_names_the_owner_the_share_needs(self):
        for uid, gid in ((99, 100), (1234, 5678)):
            for code, name in ((errno.EACCES, "EACCES"), (errno.EPERM, "EPERM")):
                with self.subTest(uid=uid, gid=gid, code=code), mock.patch("os.geteuid", return_value=uid), mock.patch("os.getegid", return_value=gid):
                    self.check(code, name, f"Darkroom cannot write to its photo share (permission denied): check the share's ownership and permissions, because Darkroom runs as uid {uid} with group {gid} and the share and its _inbox and _browser folders must be writable by that user or group, for example group {gid} with mode 2770")

    def test_a_read_only_share_says_so(self):
        self.check(errno.EROFS, "EROFS", "Darkroom's photo share is read-only: mount it read-write")

    def test_a_full_disk_says_so(self):
        self.check(errno.ENOSPC, "ENOSPC", "Darkroom's photo share is out of space: free some space on it")

    def test_a_missing_share_says_so(self):
        self.check(errno.ENOENT, "ENOENT", "Darkroom's photo share or one of its folders is missing: check that the share is mounted")

    def test_any_other_cause_points_at_the_log_and_logs_its_errno(self):
        self.check(errno.EIO, "EIO", "see the Darkroom server log for the cause")

    def test_the_log_holds_the_folder_relative_to_the_share(self):
        real = Path.mkdir

        def failing(path, *args, **kwargs):
            if path.name == "cpn1" and path.parent.name == "_inbox":
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real(path, *args, **kwargs)

        err = io.StringIO()
        with mock.patch.object(Path, "mkdir", failing), contextlib.redirect_stderr(err):
            status, _ = self.call("POST", "/api/people", {"name": self.NAME, "slug": "cpn1"})
        self.assertEqual(status, 500)
        self.assertEqual(self.folder_line(err.getvalue())["folder"], "_inbox/cpn1")
        self.assertFalse((self.photos / "cpn1").exists())

    def test_a_failure_deeper_than_the_persons_folder_logs_only_the_persons_folder(self):
        real = Path.mkdir
        cases = {"originals": "cpn1", "google-only": "cpn1", "manual": "_inbox/cpn1"}
        for leaf, expected in cases.items():
            def failing(path, *args, _leaf=leaf, **kwargs):
                if path.name == _leaf:
                    raise PermissionError(errno.EACCES, "denied", str(path))
                return real(path, *args, **kwargs)

            err = io.StringIO()
            with mock.patch.object(Path, "mkdir", failing), contextlib.redirect_stderr(err):
                status, _ = self.call("POST", "/api/people", {"name": self.NAME, "slug": "cpn1"})
            self.assertEqual(status, 500)
            line = self.folder_line(err.getvalue())
            self.assertEqual(line["folder"], expected)
            for deeper in ("originals", "google-only", "manual"):
                self.assertNotIn(deeper, json.dumps(line))
            self.assertFalse((self.photos / "cpn1").exists())

    def test_a_failed_rollback_leaves_the_folder_and_a_retry_is_refused_as_in_use(self):
        real = Path.mkdir

        def failing(path, *args, **kwargs):
            if path.name == "manual":
                raise PermissionError(errno.EACCES, "denied", str(path))
            return real(path, *args, **kwargs)

        def stuck(path):
            raise OSError(errno.EROFS, "read-only", str(path))

        err = io.StringIO()
        with mock.patch.object(Path, "mkdir", failing), mock.patch.object(Path, "rmdir", stuck), contextlib.redirect_stderr(err):
            status, reply = self.call("POST", "/api/people", {"name": self.NAME, "slug": "cpn1"})
        self.assertEqual(status, 500)
        self.assertTrue(reply["error"].startswith("Darkroom could not create the person's folders: Darkroom cannot write to its photo share (permission denied)"))
        self.assertEqual(self.folder_line(err.getvalue())["errno"], "EACCES")
        self.assertTrue((self.photos / "cpn1").is_dir())
        self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})
        status, reply = self.call("POST", "/api/people", {"name": self.NAME, "slug": "cpn1"})
        self.assertEqual(status, 409)
        self.assertIn("already in use on disk", reply["error"])

    def test_an_error_without_a_path_logs_an_unknown_folder(self):
        with mock.patch("darkroom.domain.people.Photos.ensure_person_tree", side_effect=OSError(errno.EACCES, "denied")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status, _ = self.call("POST", "/api/people", {"name": self.NAME})
        self.assertEqual(status, 500)
        self.assertEqual(self.folder_line(err.getvalue())["folder"], "unknown")


class NoSecretKeyTests(ApiCase):
    extra_env = {"DARKROOM_SECRET_KEY": ""}

    def test_a_key_is_refused_and_nothing_is_kept(self):
        person = self.person("Jo")
        with no_leaks(self, IMMICH_KEY, database=self.engine, replies=self.replies):
            status, reply = self.call("POST", "/api/people", {"name": "Sam", "immich_api_key": IMMICH_KEY})
            self.assertEqual(status, 503)
            self.assertIn("DARKROOM_SECRET_KEY", reply["error"])
            status, reply = self.call("PATCH", f"/api/people/{person['id']}", {"immich_api_key": IMMICH_KEY})
            self.assertEqual(status, 503)
        with db.session(self.engine) as opened:
            self.assertEqual([p.slug for p in opened.execute(select(Person)).scalars()], ["jo"])


class LinkedInboxTests(ApiCase):
    def test_a_person_is_not_added_through_a_linked_inbox(self):
        import shutil
        import tempfile

        elsewhere = tempfile.TemporaryDirectory()
        self.addCleanup(elsewhere.cleanup)
        inbox = self.photos / "_inbox"
        if inbox.exists():
            shutil.rmtree(inbox)
        for target in (Path(elsewhere.name), self.photos / "elsewhere"):
            with self.subTest(target=target):
                target.mkdir(exist_ok=True)
                inbox.unlink(missing_ok=True)
                inbox.symlink_to(target, target_is_directory=True)
                status, reply = self.call("POST", "/api/people", {"name": "Sam", "slug": "sam"})
                self.assertEqual(status, 409)
                self.assertIn("_inbox folder is a link", reply["error"])
                self.assertFalse((self.photos / "sam").exists())
                self.assertEqual(list(target.iterdir()), [])
                self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})

    def cleared_inbox(self):
        import shutil

        inbox = self.photos / "_inbox"
        if inbox.is_symlink():
            inbox.unlink()
        elif inbox.exists():
            shutil.rmtree(inbox)
        return inbox

    def test_a_person_is_not_added_through_a_cyclic_inbox_link(self):
        for cycle in ("self", "pair"):
            with self.subTest(cycle=cycle):
                inbox = self.cleared_inbox()
                (self.photos / "loop").unlink(missing_ok=True)
                if cycle == "self":
                    inbox.symlink_to(inbox)
                else:
                    inbox.symlink_to(self.photos / "loop")
                    (self.photos / "loop").symlink_to(inbox)
                status, reply = self.call("POST", "/api/people", {"name": "Sam", "slug": "sam"})
                self.assertEqual(status, 409)
                self.assertIn("_inbox folder is a link", reply["error"])
                self.assertFalse((self.photos / "sam").exists())
                self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})

    def test_a_link_or_folder_that_appears_while_the_person_is_added_answers_409(self):
        for linked in (True, False):
            with self.subTest(linked=linked):
                inbox = self.cleared_inbox()

                def racing(photos, slug, linked=linked, inbox=inbox):
                    if linked:
                        inbox.symlink_to(inbox)
                    raise FileExistsError(errno.EEXIST, "exists", str(inbox))

                with mock.patch("darkroom.domain.people.Photos.ensure_person_tree", autospec=True, side_effect=racing):
                    status, reply = self.call("POST", "/api/people", {"name": "Sam", "slug": "sam"})
                self.assertEqual(status, 409)
                self.assertIn("_inbox folder is a link" if linked else "appeared on disk", reply["error"])
                self.assertEqual(self.call("GET", "/api/people")[1], {"people": []})


if __name__ == "__main__":
    unittest.main()
