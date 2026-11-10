import contextlib
import io
import re
import time
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

    def test_the_worker_token_is_removed_under_its_names(self):
        for text in ("DARKROOM_BROWSER_TOKEN=worker-token-canary-0123456789abcdef", "browser_token: worker-token-canary-0123456789abcdef", "Bearer worker-token-canary-0123456789abcdef"):
            with self.subTest(text=text):
                self.assertNotIn("worker-token-canary", monitoring.scrub(text))

    def test_jwts_are_removed(self):
        token = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
        self.assertEqual(monitoring.scrub(f"saw {token} here"), "saw [removed] here")

    def test_ordinary_text_is_kept(self):
        self.assertEqual(monitoring.scrub("indexed 3 files for jo"), "indexed 3 files for jo")

    def test_credentials_in_a_database_url_are_removed(self):
        scrubbed = monitoring.scrub("cannot use postgresql+psycopg://darkroom:not%2Freal%3Dpw-canary@db:5432/darkroom")
        self.assertNotIn("pw-canary", scrubbed)
        self.assertIn("postgresql+psycopg://[removed]@db:5432/darkroom", scrubbed)


PREVIOUS_URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+@")
PREVIOUS_URL_QUERY = re.compile(r"(https?://[^\s?#\"']+)\?[^\s#\"']*")
PREVIOUS_BEARER = re.compile(r"(?i)\b(bearer|basic|x-api-key)[:=\s]+\S+")
PREVIOUS_NAMED_SECRET = re.compile(r"(?i)\b(code|state|nonce|token|id_token|access_token|refresh_token|client_secret|code_verifier|session|cookie|set-cookie|authorization|browser_token|darkroom_browser_token)([:=]\s*)[^\s;&,\"']+")
PREVIOUS_JWT = re.compile(r"\beyJ[\w-]{4,}\.[\w-]+\.[\w-]*")


def previous_scrub(text):
    text = PREVIOUS_URL_CREDENTIALS.sub(r"\1[removed]@", text)
    text = PREVIOUS_URL_QUERY.sub(r"\1?[removed]", text)
    text = PREVIOUS_BEARER.sub(r"\1 [removed]", text)
    text = PREVIOUS_NAMED_SECRET.sub(r"\1\2[removed]", text)
    return PREVIOUS_JWT.sub("[removed]", text)


JWT_TEXT = "eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.c2lnbmF0dXJl"
SAMPLES = (
    "https://user:hunter2@host/x",
    "GET https://id.example/callback?code=CODE-1234&state=STATE-5678 HTTP/1.1",
    "see (https://u:pw@h/a?b=c#frag) and http://plain.example/path",
    "http://a/http://b?x=1 http://a#http://b?y=2 http://?http://c?z=3",
    "http://a?b?c 'https://q.example/p?k=v' \"http://r.example/?s\"",
    "nothttps://u:p@h/x 1http://u:p@h/x",
    "Authorization: Bearer abc123, x-api-key=k-1, basic::",
    "Cookie: darkroom_session=SESSION-9999; token :  TOK-1",
    f"saw {JWT_TEXT} here",
    f"abc-{JWT_TEXT}.rest",
    f"eyJab.-{JWT_TEXT} eyJ-x-{JWT_TEXT}",
    "eyJshort.a.b eyJlonger.payload eyJlonger.payload.",
    "indexed 3 files for jo",
)
ADVERSARIAL = (
    "http://" * 40_000,
    "https://a" * 30_000,
    "eyJaaaa-" * 35_000,
    "eyJaaaa.eyJbbbb-" * 18_000,
    "a" * 280_000,
    "a+" * 140_000 + "://",
    "x://" * 70_000,
    "token=" + " " * 280_000,
    "bearer" + " " * 280_000,
    "Bad request syntax ('GET /" + "http://" * 40_000 + "')",
)


class LinearScrubTests(unittest.TestCase):
    def test_redacts_what_it_redacted_before(self):
        for text in SAMPLES:
            with self.subTest(text=text):
                self.assertEqual(monitoring.scrub(text), previous_scrub(text))

    def test_a_huge_adversarial_line_is_scrubbed_quickly(self):
        for text in ADVERSARIAL:
            with self.subTest(text=text[:24]):
                started = time.perf_counter()
                monitoring.scrub(text)
                self.assertLess(time.perf_counter() - started, 2.0)

    def test_a_huge_request_line_is_logged_quickly_and_still_scrubbed(self):
        line = "Bad request syntax ('GET /?code=CODE-CANARY-1 " + "http://" * 40_000 + " https://u:pw-canary@h/x')"
        started = time.perf_counter()
        with contextlib.redirect_stderr(io.StringIO()) as err:
            monitoring.log("request", method="GET", status=line)
        self.assertLess(time.perf_counter() - started, 2.0)
        self.assertNotIn("CODE-CANARY-1", err.getvalue())
        self.assertNotIn("pw-canary", err.getvalue())


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
