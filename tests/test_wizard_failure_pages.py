import html
import re
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from darkroom.server import wizard
from tests.test_wizard_api import WizardCase

BACK_TO_SETUP = '<a class="button" href="/setup">Back to setup</a>'
NOT_STARTED_HERE = "This test sign-in did not start here, so nothing was saved."


def alert(body):
    match = re.search(r'<div role="alert" class="alert"><b><i></i>(.*?)</b><span>(.*?)</span></div>', body.decode())
    assert match is not None, body
    return html.unescape(match[1]), html.unescape(match[2])


class FailurePageTests(WizardCase):
    def started_test(self):
        status, reply = self.json_call("POST", "/api/setup/sign-in/test")
        self.assertEqual(status, 200, reply)
        return {name: values[0] for name, values in parse_qs(urlsplit(reply["location"]).query).items()}

    def come_back(self, query, code, cookies=None):
        jar = cookies if cookies is not None else {wizard.TEST_COOKIE: query["state"], wizard.CLAIM_COOKIE: self.cookie}
        return self.call("GET", f"/auth/callback?code={code}&state={query['state']}", cookies=jar)

    def assert_page(self, reply, status, title, text, linked):
        self.assertEqual(reply[0], status)
        body = reply[2]
        self.assertEqual(alert(body), (title, text))
        self.assertEqual(body.decode().count("<a "), 1 if linked else 0)
        self.assertEqual(BACK_TO_SETUP in body.decode(), linked)
        self.assertIn(f"{wizard.TEST_COOKIE}=; Path=/auth; Max-Age=0", "\n".join(reply[1].get_all("Set-Cookie")))
        self.assertEqual(self.stored(), {})

    def test_a_refused_account_links_back_to_setup(self):
        self.claim()
        self.propose(oidc_allowed_groups=["admins"])
        self.assert_page(self.try_sign_in(groups=["family"]), 403, "Not allowed",
                         "The account you signed in with is not allowed by the proposed settings, so nothing was saved. "
                         "Check the allowed groups and email addresses, then test again.", linked=True)

    def test_a_failed_sign_in_links_back_to_setup(self):
        self.claim()
        self.propose()
        query = self.started_test()
        self.assert_page(self.come_back(query, "test-code-never-issued-5d6e"), 403, "The test sign-in failed",
                         "Something was wrong with this sign-in: the provider answered 400. Nothing was saved.", linked=True)

    def test_settings_changed_during_the_test_link_back_to_setup(self):
        self.claim()
        self.propose()
        query = self.started_test()
        self.provider.issue("test-code-late-1234", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"])
        real_load = wizard.load_proposal
        loads = []

        def changed_on_the_second_load(opened, key):
            fields = real_load(opened, key)
            loads.append(fields)
            return fields if len(loads) == 1 else {**fields, "id": "changed-during-the-test"}

        with mock.patch.object(wizard, "load_proposal", side_effect=changed_on_the_second_load):
            reply = self.come_back(query, "test-code-late-1234")
        self.assertEqual(len(loads), 2)
        self.assert_page(reply, 409, "The settings changed",
                         "The proposed sign-in settings changed during the test, so nothing was saved. Test again.", linked=True)

    def test_nothing_to_test_links_back_to_setup(self):
        self.claim()
        self.propose()
        query = self.started_test()
        with mock.patch.object(wizard, "load_proposal", return_value=None):
            reply = self.come_back(query, "x")
        self.assert_page(reply, 409, "Nothing to test", "There are no proposed sign-in settings to test, so nothing was saved.", linked=True)

    def test_a_test_from_another_browser_has_no_link(self):
        self.claim()
        self.propose()
        query = self.started_test()
        self.assert_page(self.come_back(query, "x", cookies={wizard.TEST_COOKIE: query["state"]}), 403, "Not allowed", NOT_STARTED_HERE, linked=False)

    def test_a_window_that_moved_on_during_the_test_has_no_link(self):
        self.claim()
        self.propose()
        query = self.started_test()
        self.provider.issue("test-code-moved-on-7a8b", nonce=query["nonce"], challenge=query["code_challenge"], groups=["parents"])
        moved_on = mock.Mock(phase="after_test")
        with mock.patch.object(wizard, "locked_window", return_value=moved_on):
            reply = self.come_back(query, "test-code-moved-on-7a8b")
        self.assert_page(reply, 403, "Not allowed", NOT_STARTED_HERE, linked=False)

    def test_the_link_is_the_fixed_setup_page(self):
        self.assertEqual(wizard.BACK_TO_SETUP, ("/setup", "Back to setup"))
        self.assertEqual(wizard.SETUP_PAGE, "/setup")


if __name__ == "__main__":
    unittest.main()
