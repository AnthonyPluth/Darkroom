import unittest

from darkroom_browser.pages import takeout

SIGNED_IN = """<!doctype html><html><head><title>Google Takeout</title><script>var x = "sign in to continue";</script></head>
<body><nav><a aria-label="Google Account: Jo (jo@example.test)">J</a></nav>
<main><h1>Your Google data</h1><h2>Create a new export</h2><p>Select data to include</p>
<section><h2>Manage your exports</h2><ul role="list"><li>Export from 3 Jan</li></ul></section></main></body></html>"""

SIGN_IN_NEEDED = """<!doctype html><html><head><title>Sign in - Google Accounts</title></head>
<body><div id="initialView"><h1>Sign in</h1><p>to continue to Google Takeout</p>
<form action="https://accounts.google.com/v3/signin/identifier?continue=https://takeout.google.com">
<input type="email" id="identifierId" aria-label="Email or phone"></form></div></body></html>"""

ACCOUNT_CHOOSER = """<html><head><title>Choose an account</title></head><body><h1>Choose an account</h1><p>to continue to Google Takeout</p>
<ul><li>jo@example.test</li><li>Use another account</li></ul></body></html>"""

VERIFY = """<!doctype html><html><head><title>Google Accounts</title></head>
<body><h1>Verify it&#8217;s you</h1><p>To help keep your account safe, Google wants to make sure it&#8217;s really you trying to sign in</p>
<ul><li>Tap Yes on your phone</li></ul></body></html>"""

VERIFY_PLAIN = "<html><body><h1>Confirm it's you</h1><p>Try another way</p></body></html>"

UNKNOWN = "<html><head><title>Google</title></head><body><h1>Something went wrong</h1><p>Please try again later.</p></body></html>"


class ClassifyTests(unittest.TestCase):
    def test_signed_in(self):
        self.assertEqual(takeout.classify(SIGNED_IN), takeout.SIGNED_IN)

    def test_sign_in_needed_by_form_and_by_text(self):
        self.assertEqual(takeout.classify(SIGN_IN_NEEDED), takeout.SIGN_IN_NEEDED)
        self.assertEqual(takeout.classify(ACCOUNT_CHOOSER), takeout.SIGN_IN_NEEDED)
        self.assertEqual(takeout.classify('<form><input type="email"></form>'), takeout.SIGN_IN_NEEDED)

    def test_verify_its_you_wins_over_sign_in(self):
        self.assertEqual(takeout.classify(VERIFY), takeout.VERIFY_ITS_YOU)
        self.assertEqual(takeout.classify(VERIFY_PLAIN), takeout.VERIFY_ITS_YOU)
        self.assertEqual(takeout.classify(VERIFY + SIGN_IN_NEEDED), takeout.VERIFY_ITS_YOU)

    def test_unknown(self):
        self.assertEqual(takeout.classify(UNKNOWN), takeout.UNKNOWN)
        self.assertEqual(takeout.classify(""), takeout.UNKNOWN)
        self.assertEqual(takeout.classify("<p>Manage your exports</p>"), takeout.UNKNOWN, "signed-in needs the Takeout page itself")

    def test_scripts_and_styles_do_not_count_as_visible_text(self):
        page = "<html><head><script>document.title = 'verify it is you';</script><style>.x{}</style></head><body><h1>Takeout</h1><p>Manage your exports</p></body></html>"
        self.assertEqual(takeout.classify(page), takeout.SIGNED_IN)
        self.assertEqual(takeout.visible_text("<p>A&amp;B</p>\n<p> c </p>"), " a&b c ")

    def test_signed_in_takeout_ignores_sign_in_urls_in_scripts_and_links(self):
        for address in ("https://accounts.google.com/signin", "https://accounts.google.com/v3/signin/identifier", "https://accounts.google.com/AccountChooser"):
            with self.subTest(address=address):
                page = SIGNED_IN.replace("</head>", f'<script>const accountChooser = "{address}"; const identifierId = null;</script></head>')
                page = page.replace("</nav>", f'<a href="{address}">Switch account</a></nav>')
                self.assertEqual(takeout.classify(page), takeout.SIGNED_IN)

    def test_actual_sign_in_forms_still_win_over_takeout_return_links(self):
        for form in ('<form action="https://accounts.google.com/v3/signin/identifier"></form>', '<input TYPE="email">', '<input id="identifierId">'):
            with self.subTest(form=form):
                self.assertEqual(takeout.classify(SIGNED_IN + form), takeout.SIGN_IN_NEEDED)


NAME_CANARY = "Jocanary Quillfeather"
EMAIL_CANARY = "quill.canary0913@example.test"
TOKEN_CANARY = "AHWqTUcanaryToken4471"
REAUTH_URL = f"https://accounts.google.com/v3/signin/challenge/pwd?TL={TOKEN_CANARY}&continue=https%3A%2F%2Ftakeout.google.com"
REAUTH_UNWORDED = f"""<html><head><title>{NAME_CANARY}</title></head><body><h1>{NAME_CANARY}</h1><div><span>{EMAIL_CANARY}</span></div>
<p>Weiter</p><button>Weiter</button></body></html>"""
REAUTH_WELCOME = f"""<html><head><title>Google Accounts</title></head><body><h1>Hi {NAME_CANARY}</h1>
<div data-email="{EMAIL_CANARY}" id="profileIdentifier">{EMAIL_CANARY}</div><button>Next</button></body></html>"""
SECRET_FIELD_ONLY = f"""<html><body><form method="post" action="/x?{TOKEN_CANARY}"><input type="hidden" name="TL" value="{TOKEN_CANARY}">
<input name="Passwd" autocomplete="current-password"></form></body></html>"""
SECRET_INPUT = '<html><body><div role="dialog"><input TYPE="Password" aria-label="x"></div></body></html>'
ODD_PAGE = f"<html><head><title>{NAME_CANARY}</title></head><body><h1>Something went wrong</h1><h2>{EMAIL_CANARY}</h2><p>{TOKEN_CANARY}</p></body></html>"
TAKEOUT_ADDRESS = "https://takeout.google.com/manage/archive/x"


class ClassifyAfterAClickTests(unittest.TestCase):
    def test_any_page_on_googles_accounts_host_is_one_the_owner_must_act_on_whatever_it_says(self):
        for page in (REAUTH_UNWORDED, UNKNOWN, ""):
            with self.subTest(page=page[:30]):
                self.assertEqual(takeout.classify_at(page, REAUTH_URL), takeout.VERIFY_ITS_YOU)
        self.assertEqual(takeout.classify_at(REAUTH_UNWORDED, "https://ACCOUNTS.google.com/"), takeout.VERIFY_ITS_YOU)

    def test_a_password_field_alone_is_enough_on_any_host(self):
        for page in (SECRET_FIELD_ONLY, SECRET_INPUT, SIGNED_IN.replace("</main>", SECRET_INPUT + "</main>")):
            for address in (TAKEOUT_ADDRESS, "https://other.example/", "about:blank"):
                with self.subTest(page=page[:30], address=address):
                    self.assertEqual(takeout.classify_at(page, address), takeout.VERIFY_ITS_YOU)

    def test_a_password_field_inside_a_script_does_not_count(self):
        page = SIGNED_IN.replace("</head>", '<script>const f = \'<input type="password">\';</script></head>')
        self.assertEqual(takeout.classify_at(page, TAKEOUT_ADDRESS), takeout.SIGNED_IN)

    def test_a_greeting_or_identity_chip_off_takeout_asks_for_the_owner(self):
        self.assertEqual(takeout.classify_at(REAUTH_WELCOME, "https://other.example/"), takeout.VERIFY_ITS_YOU)
        self.assertEqual(takeout.classify_at("<html><body><h1>Welcome</h1><button>Next</button></body></html>", "https://other.example/"), takeout.VERIFY_ITS_YOU)

    def test_re_authentication_wording_asks_for_the_owner(self):
        for wording in ("Enter your password", "To continue, first verify it’s you", "Confirm your password"):
            with self.subTest(wording=wording):
                self.assertEqual(takeout.classify_at(f"<html><body><p>{wording}</p></body></html>", "https://other.example/"), takeout.VERIFY_ITS_YOU)

    def test_the_existing_phrases_and_forms_still_decide_first(self):
        self.assertEqual(takeout.classify_at(VERIFY, TAKEOUT_ADDRESS), takeout.VERIFY_ITS_YOU)
        self.assertEqual(takeout.classify_at(SIGN_IN_NEEDED, TAKEOUT_ADDRESS), takeout.SIGN_IN_NEEDED)

    def test_takeouts_own_signed_in_page_with_its_account_chip_stays_signed_in(self):
        page = SIGNED_IN.replace("</nav>", f'<div data-email="{EMAIL_CANARY}">J</div></nav>')
        self.assertEqual(takeout.classify_at(page, TAKEOUT_ADDRESS), takeout.SIGNED_IN)
        self.assertEqual(takeout.classify_at(UNKNOWN, TAKEOUT_ADDRESS), takeout.UNKNOWN)
        self.assertEqual(takeout.classify_at(ODD_PAGE, "https://www.google.com/sorry"), takeout.UNKNOWN)

    def test_hosts_are_told_apart_by_the_real_host_only(self):
        cases = {
            "https://takeout.google.com/manage": "takeout",
            REAUTH_URL: "accounts",
            "https://accounts.google.com.evil.example/": "other",
            "https://evil.example/?next=https://accounts.google.com/": "other",
            "https://accounts.google.com@evil.example/": "other",
            "https://jo@accounts.google.com/": "other",
            "http://accounts.google.com/": "other",
            "https://accounts.google.com\\@evil.example/": "other",
            "chrome-error://chromewebdata/": "other",
            "https://[::1/": "other",
            "about:blank": "none",
            "": "none",
            "  ": "none",
        }
        for address, category in cases.items():
            with self.subTest(address=address):
                self.assertEqual(takeout.host_category(address), category)

    def test_what_a_click_led_to_is_said_in_fixed_words_without_page_text(self):
        said = takeout.click_findings(ODD_PAGE, REAUTH_URL, 2, False)
        self.assertEqual(said, "another tab: page host accounts; no password field; an unrecognised title; 2 other headings; 2 tabs open")
        self.assertEqual(takeout.click_findings(SECRET_FIELD_ONLY, TAKEOUT_ADDRESS, 1, True), "the job's own tab: page host takeout; a password field; no title or headings; 1 tab open")
        self.assertEqual(takeout.click_findings("", "", 0, True), "the job's own tab: page host none; no password field; no title or headings; 0 tabs open")
        for page in (ODD_PAGE, REAUTH_WELCOME, REAUTH_UNWORDED, SECRET_FIELD_ONLY):
            said = takeout.click_findings(page, REAUTH_URL, 1, True)
            for canary in (NAME_CANARY, NAME_CANARY.lower(), EMAIL_CANARY, TOKEN_CANARY, "quillfeather", "challenge"):
                self.assertNotIn(canary, said)


PART = "https://takeout.google.com/takeout/download?j=export-1&i=0&user=made-up"


class DownloadOfThePartTests(unittest.TestCase):
    NAME = "takeout-20261102T031000Z-001.zip"
    REDIRECTED = "https://takeout-download.usercontent.google.com/download/takeout-20261102T031000Z-001.zip"

    def test_a_download_naming_this_export_and_part_is_this_parts_on_any_https_host(self):
        for url in (PART, "https://takeout.google.com/takeout/download?i=0&j=export-1", self.REDIRECTED + "?j=export-1&i=0&user=made-up"):
            with self.subTest(url=url):
                self.assertTrue(takeout.download_of_part(url, self.NAME, PART, 1))

    def test_a_download_naming_another_export_or_part_is_not(self):
        for url in ("https://takeout.google.com/takeout/download?j=export-1&i=1&user=made-up", "https://takeout.google.com/takeout/download?j=export-2&i=0",
                    self.REDIRECTED + "?j=export-1&i=1", self.REDIRECTED + "?j=export-2&i=0", self.REDIRECTED + "?i=0", self.REDIRECTED + "?j=export-1",
                    "https://takeout.google.com/takeout/download?j=export-1&i=0&%6a=export-2", "https://takeout.google.com/takeout/download?j=export-1&i=0&i=1"):
            with self.subTest(url=url):
                self.assertFalse(takeout.download_of_part(url, self.NAME, PART, 1))

    def test_without_ids_the_file_name_must_carry_this_parts_number(self):
        self.assertTrue(takeout.download_of_part(self.REDIRECTED, self.NAME, PART, 1))
        self.assertTrue(takeout.download_of_part(self.REDIRECTED, "takeout-20261102T031000Z-012.zip", PART, 12))
        for name in ("takeout-20261102T031000Z-002.zip", "takeout.zip", "photos-001.zip.exe", "", None, "../x/takeout-002.zip"):
            with self.subTest(name=name):
                self.assertFalse(takeout.download_of_part(self.REDIRECTED, name, PART, 1))

    def test_an_address_that_is_not_plain_https_or_is_off_takeouts_download_path_is_not(self):
        for url in ("http://takeout-download.usercontent.google.com/x?j=export-1&i=0", "https://takeout.google.com/settings?j=export-1&i=0",
                    "https://accounts.google.com/x?j=export-1&i=0", "https://takeout.google.com\\@other.example/takeout/download?j=export-1&i=0",
                    "https://user@other.example/x?j=export-1&i=0", "", None, 7):
            with self.subTest(url=url):
                self.assertFalse(takeout.download_of_part(url, self.NAME, PART, 1))
        self.assertFalse(takeout.download_of_part(PART, self.NAME, "https://takeout.google.com/takeout/download?j=export-1", 1))


if __name__ == "__main__":
    unittest.main()
