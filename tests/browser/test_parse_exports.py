import unittest
from datetime import UTC, datetime

from darkroom_browser.pages import takeout
from tests.snapshots import snapshot_pending

GB = 1024**3


def stamp(*when):
    return datetime(*when, tzinfo=UTC).timestamp()


def page(*items, nav="<ul><li>Home</li><li>Help</li></ul>"):
    return f"""<!doctype html><html><head><title>Google Takeout</title><script>var s = "<li data-export-id='trap'>expired</li>";</script></head>
<body>{nav}<main><h1>Your Google data</h1><h2>Manage your exports</h2><ul role="list">{"".join(items)}</ul></main></body></html>"""


def ready(export_id="aaaa-1111", parts=((1, "49.8 GB"), (2, "49.9 GB"), (3, "12.4 GB")), created="2026-11-02T03:10:00Z", expires="2026-11-09T03:10:00Z"):
    links = "".join(f'<a href="https://takeout.google.com/takeout/download?j={export_id}&amp;i={number - 1}" aria-label="Download part {number} of {len(parts)} ({size})">Download</a>' for number, size in parts)
    return f"""<li role="listitem" data-export-id="{export_id}"><h3>Google Photos export</h3><p>Created <time data-time="created" datetime="{created}">2 Nov 2026</time></p>
<p>Files expire on <time data-time="expires" datetime="{expires}">9 Nov 2026</time></p><div>{links}</div></li>"""


def in_progress(export_id="bbbb-2222", created="2026-12-02T03:10:00Z"):
    return f"""<li data-export-id="{export_id}"><h3>Google Photos export</h3><p>Created <time data-time="created" datetime="{created}">2 Dec 2026</time></p>
<p>Your export is in progress. We will email you when it is ready.</p></li>"""


def expired(export_id="cccc-3333", created="2026-09-02T03:10:00Z", expires="2026-09-09T03:10:00Z"):
    return f"""<li data-export-id="{export_id}"><h3>Google Photos export</h3><p>Created <time data-time="created" datetime="{created}">2 Sep 2026</time></p>
<p>This export expired on <time data-time="expires" datetime="{expires}">9 Sep 2026</time>. The files are no longer available.</p></li>"""


@snapshot_pending
class ParseExportsTests(unittest.TestCase):
    def test_a_ready_multi_part_export(self):
        found = takeout.parse_exports(page(ready()))
        self.assertEqual(len(found), 1)
        export = found[0]
        self.assertEqual((export.google_id, export.state), ("aaaa-1111", "ready"))
        self.assertEqual((export.created_at, export.expires_at), (stamp(2026, 11, 2, 3, 10), stamp(2026, 11, 9, 3, 10)))
        self.assertEqual([part.number for part in export.parts], [1, 2, 3])
        self.assertEqual([part.bytes for part in export.parts], [round(49.8 * GB), round(49.9 * GB), round(12.4 * GB)])
        self.assertTrue(all(part.href.startswith("https://takeout.google.com/takeout/download?j=aaaa-1111&i=") for part in export.parts))

    def test_an_export_in_progress_has_no_parts(self):
        export = takeout.parse_exports(page(in_progress()))[0]
        self.assertEqual((export.google_id, export.state, export.expires_at, export.parts), ("bbbb-2222", "in_progress", None, ()))

    def test_an_expired_export(self):
        export = takeout.parse_exports(page(expired()))[0]
        self.assertEqual((export.google_id, export.state, export.expires_at, export.parts), ("cccc-3333", "expired", stamp(2026, 9, 9, 3, 10), ()))

    def test_several_exports_come_back_in_page_order_and_the_navigation_list_is_ignored(self):
        found = takeout.parse_exports(page(in_progress(), ready(), expired()))
        self.assertEqual([(each.google_id, each.state) for each in found], [("bbbb-2222", "in_progress"), ("aaaa-1111", "ready"), ("cccc-3333", "expired")])

    def test_the_id_falls_back_to_the_download_link(self):
        item = ready().replace(' data-export-id="aaaa-1111"', "")
        self.assertEqual(takeout.parse_exports(page(item))[0].google_id, "aaaa-1111")

    def test_a_single_unnumbered_link_is_part_one_and_an_offset_is_honoured(self):
        item = ready(parts=((1, "3 MB"),), created="2026-11-02T05:10:00+02:00").replace("Download part 1 of 1 (3 MB)", "Download (3 MB)")
        export = takeout.parse_exports(page(item))[0]
        self.assertEqual((export.parts[0].number, export.parts[0].bytes, export.created_at), (1, 3 * 1024**2, stamp(2026, 11, 2, 3, 10)))

    def test_a_date_with_no_time_zone_is_refused(self):
        with self.assertRaises(takeout.UnreadableExports):
            takeout.parse_exports(page(ready(created="2026-11-02T03:10:00")))

    def test_links_to_other_places_are_not_parts(self):
        item = ready().replace("</div>", '<a href="https://example.test/takeout/download?j=x" aria-label="Download part 9 (1 GB)">x</a><a href="https://takeout.google.com/settings">Settings</a></div>')
        self.assertEqual([part.number for part in takeout.parse_exports(page(item))[0].parts], [1, 2, 3])

    def test_a_page_with_no_exports_is_an_empty_list(self):
        self.assertEqual(takeout.parse_exports(page()), [])

    def test_the_page_must_be_the_exports_page(self):
        for text in ("", "<html><body><h1>Create a new export</h1></body></html>"):
            with self.subTest(text=text), self.assertRaises(takeout.UnreadableExports):
                takeout.parse_exports(text)
        self.assertFalse(takeout.exports_page_ready("<p>nothing</p>"))
        self.assertTrue(takeout.exports_page_ready(page()))

    def test_anything_unreadable_is_refused_with_a_fixed_message_naming_no_page_text(self):
        canary = "CANARY-PAGE-TEXT"
        broken = {
            "unrecognised state": f'<li data-export-id="x"><time data-time="created" datetime="2026-11-02T03:10:00Z"></time><p>{canary}</p></li>',
            "no creation date": f'<li data-export-id="x"><p>in progress {canary}</p></li>',
            "bad date": f'<li data-export-id="x"><time data-time="created" datetime="{canary}"></time><p>in progress</p></li>',
            "no size": ready().replace("(49.8 GB)", f"({canary})"),
            "repeated part": ready(parts=((1, "1 GB"), (1, "2 GB"))),
            "no id": ready().replace(' data-export-id="aaaa-1111"', "").replace("?j=aaaa-1111&amp;i=", "?i="),
            "repeated export": ready() + ready(),
        }
        for name, item in broken.items():
            with self.subTest(name), self.assertRaises(takeout.UnreadableExports) as raised:
                takeout.parse_exports(page(item))
            self.assertNotIn(canary, str(raised.exception))

    def test_too_many_exports_are_refused(self):
        items = [in_progress(export_id=f"id-{number}") for number in range(takeout.MAX_EXPORTS + 1)]
        with self.assertRaises(takeout.UnreadableExports):
            takeout.parse_exports(page(*items))

    def test_div_lists_and_nested_markup_are_read(self):
        item = '<div role="listitem" data-export-id="d1"><span><b>Created</b> <time data-time="created" datetime="2026-11-02T03:10:00Z">x</time><br/><img src="x.png"></span><p>being created</p></div>'
        text = f'<html><body><h2>Manage your exports</h2><div role="list">{item}</div><section><p>Footer</p></section></body></html>'
        self.assertEqual([(each.google_id, each.state) for each in takeout.parse_exports(text)], [("d1", "in_progress")])


if __name__ == "__main__":
    unittest.main()
