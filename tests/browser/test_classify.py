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


if __name__ == "__main__":
    unittest.main()
