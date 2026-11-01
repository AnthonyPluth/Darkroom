import contextlib
import io
import unittest

from darkroom import monitoring


class ScrubTests(unittest.TestCase):
    def test_credentials_in_urls_and_headers_are_removed(self):
        self.assertNotIn("hunter2", monitoring.scrub("https://user:hunter2@host/x"))
        self.assertNotIn("abc123", monitoring.scrub("x-api-key: abc123"))
        self.assertNotIn("abc123", monitoring.scrub("Authorization: Bearer abc123"))
        self.assertNotIn("dXNlcjpwYXNz", monitoring.scrub("Authorization: Basic dXNlcjpwYXNz"))

    def test_query_strings_and_named_secrets_are_removed(self):
        scrubbed = monitoring.scrub("GET https://id.example/callback?code=CODE-1234&state=STATE-5678")
        self.assertNotIn("CODE-1234", scrubbed)
        self.assertNotIn("STATE-5678", scrubbed)
        self.assertIn("https://id.example/callback?[removed]", scrubbed)
        for text in ("code=CODE-1234", "id_token=tok.en.x", "Cookie: darkroom_session=SESSION-9999", "client_secret=S3CRET", "token: TOK-1", "refresh_token=refresh-canary-abcdef"):
            with self.subTest(text=text):
                self.assertNotIn(text.split("=")[-1].split(": ")[-1], monitoring.scrub(text))

    def test_jwts_are_removed(self):
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
        self.assertEqual(monitoring.scrub(f"saw {token} here"), "saw [removed] here")

    def test_ordinary_text_is_kept(self):
        self.assertEqual(monitoring.scrub("indexed 3 files for jo"), "indexed 3 files for jo")


class LogTests(unittest.TestCase):
    def test_log_writes_one_scrubbed_json_line(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.log("synced", count=3, note="bearer secret-token")
        written = err.getvalue()
        self.assertEqual(written.count("\n"), 1)
        self.assertIn('"count": 3', written)
        self.assertNotIn("secret-token", written)


class ReportTests(unittest.TestCase):
    def failing(self):
        secret = "TOP-" + "SECRET-VALUE"
        try:
            raise ValueError(f"token={secret}")
        except ValueError as error:
            raise RuntimeError(f"while signing in: {secret}") from error

    def test_report_keeps_types_and_drops_values_by_default(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                self.failing()
            except RuntimeError:
                monitoring.report()
        text = err.getvalue()
        self.assertIn("ValueError", text)
        self.assertIn("RuntimeError", text)
        self.assertIn("another exception was raised", text)
        self.assertNotIn("TOP-SECRET-VALUE", text)

    def test_report_with_values_still_scrubs(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            try:
                raise ValueError("code=CODE-ABCDEF https://u:pw@h/x")
            except ValueError as error:
                monitoring.report(error, values=True)
        text = err.getvalue()
        self.assertIn("ValueError", text)
        self.assertNotIn("CODE-ABCDEF", text)
        self.assertNotIn("u:pw@", text)

    def test_report_outside_an_exception_writes_nothing(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.report()
        self.assertEqual(err.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
