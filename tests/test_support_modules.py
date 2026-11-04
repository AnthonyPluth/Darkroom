import unittest
from unittest import mock

from darkroom import dates, monitoring, oidc, tls, validate
from darkroom.config import load


class ValidateTests(unittest.TestCase):
    def test_json_object(self):
        self.assertEqual(validate.json_object(b'{"a": 1}'), {"a": 1})
        self.assertEqual(validate.json_object(b""), {})
        for bad in (b"[1]", b"{", b"x" * 20):
            with self.assertRaises(validate.Invalid):
                validate.json_object(bad, limit=10)

    def test_text(self):
        self.assertEqual(validate.text({"n": " Jo "}, "n"), "Jo")
        self.assertEqual(validate.text({}, "n", required=False), "")
        for body in ({}, {"n": 3}, {"n": "x" * 300}, {"n": "   "}):
            with self.assertRaises(validate.Invalid):
                validate.text(body, "n")
        self.assertEqual(validate.text({"n": "  "}, "n", required=False), "")

    def test_present_and_text_or_none(self):
        self.assertTrue(validate.present({"n": None}, "n"))
        self.assertFalse(validate.present({}, "n"))
        self.assertIsNone(validate.text_or_none({"n": None}, "n"))
        self.assertIsNone(validate.text_or_none({"n": "  "}, "n"))
        self.assertEqual(validate.text_or_none({"n": " a "}, "n"), "a")
        with self.assertRaises(validate.Invalid):
            validate.text_or_none({"n": 4}, "n")

    def test_whole_numbers(self):
        self.assertEqual(validate.whole_numbers({"n": [3, 1]}, "n"), [3, 1])
        for body in ({}, {"n": 3}, {"n": [True]}, {"n": ["1"]}, {"n": [0]}, {"n": [1, 1]}, {"n": list(range(1, 60))}):
            with self.assertRaises(validate.Invalid):
                validate.whole_numbers(body, "n")

    def test_email(self):
        self.assertEqual(validate.email({"e": " jo@example.test "}, "e"), "jo@example.test")
        for body in ({}, {"e": "jo"}, {"e": "@example.test"}, {"e": "jo@example"}, {"e": "jo@.test"}, {"e": "jo@test."}, {"e": "j o@example.test"}, {"e": "a@b@example.test"}, {"e": "<jo>@example.test"}):
            with self.assertRaises(validate.Invalid):
                validate.email(body, "e")

    def test_whole_number(self):
        self.assertEqual(validate.whole_number({"n": 3}, "n"), 3)
        for body in ({"n": True}, {"n": "3"}, {"n": -1}, {}):
            with self.assertRaises(validate.Invalid):
                validate.whole_number(body, "n")


class MonitoringTests(unittest.TestCase):
    def test_scrub_removes_credentials_and_keys(self):
        self.assertNotIn("hunter2", monitoring.scrub("https://user:hunter2@host/x"))
        self.assertNotIn("abc123", monitoring.scrub("x-api-key: abc123"))

    def test_log_writes_one_scrubbed_json_line(self):
        with mock.patch("sys.stderr") as stderr:
            monitoring.log("synced", count=3, note="bearer secret-token")
        written = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('"count": 3', written)
        self.assertNotIn("secret-token", written)


class TlsTests(unittest.TestCase):
    def test_http_is_refused_unless_allowed(self):
        with self.assertRaises(tls.Refused):
            tls.urlopen("http://192.168.1.2/x")
        with self.assertRaises(tls.Refused):
            tls.urlopen("ftp://host/x", allow_http=True)

    def test_http_is_opened_when_allowed(self):
        opener = mock.Mock()
        opener.open.return_value = mock.Mock(spec=tls.HTTPResponse)
        with mock.patch("urllib.request.build_opener", return_value=opener):
            tls.urlopen("http://192.168.1.2/x", allow_http=True)
        opener.open.assert_called_once()

    def test_a_redirect_is_refused_instead_of_followed(self):
        handler = tls.NoRedirects()
        with self.assertRaises(tls.Refused):
            handler.redirect_request(mock.Mock(), mock.Mock(), 302, "Found", {}, "http://elsewhere.example/")


class DatesTests(unittest.TestCase):
    def test_utc_now_is_aware(self):
        self.assertIsNotNone(dates.utc_now().tzinfo)

    def test_local_today_is_an_iso_date(self):
        self.assertRegex(dates.local_today("UTC"), r"^\d{4}-\d{2}-\d{2}$")


class OidcTests(unittest.TestCase):
    def test_configured_needs_issuer_and_client(self):
        with mock.patch.dict("os.environ", {"OIDC_ISSUER": "https://id.example", "OIDC_CLIENT_ID": "x"}, clear=True):
            self.assertTrue(oidc.configured(load()))
        with mock.patch.dict("os.environ", {}, clear=True):
            self.assertFalse(oidc.configured(load()))


if __name__ == "__main__":
    unittest.main()
