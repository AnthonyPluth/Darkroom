import itertools
import json
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest import mock
from zoneinfo import ZoneInfo

from darkroom_browser.pages import takeout, takeout_data
from tests.browser.takeout_pages import (COMPLETED_ID, EXPIRED_ID, IN_PROGRESS_ID, MANAGE_PAGE, SIZES, archive, callback, completed_row, detail_blocks, downloaded_page_parts,
                                         embedded_export, embedded_part, embedded_parts, expired_row, in_progress_row, list_blocks, part_item, part_names, row, summary,
                                         with_blocks)
from tests.browser.test_parse_exports import ARCHIVE_PAGE, CANARY, NEW_YORK, NOW, at

CREATED_MS = int((NOW - 3600) * 1000) + 30_000
EXPIRES_MS = int(at(2026, 11, 24, 13, 55) * 1000) + 45_000
EXPIRED_CREATED_MS = int(at(2026, 11, 9, 14, 45) * 1000) + 12_000
EXPIRED_EXPIRES_MS = int(at(2026, 11, 16, 14, 45) * 1000)
IN_PROGRESS_CREATED_MS = int((NOW - 300) * 1000)
FIXED_REASONS = {takeout_data.NONE_ON_THE_PAGE, takeout_data.TOO_LARGE, takeout_data.TOO_DEEP, takeout_data.TOO_MANY, takeout_data.NOT_DATA, takeout_data.UNCHECKED}
NAMES = part_names(3)


def completed(google_id=COMPLETED_ID, created_ms=CREATED_MS, expires_ms=EXPIRES_MS, parts=None):
    return embedded_export(google_id, created_ms, expires_ms, embedded_parts() if parts is None else parts, total=sum(SIZES))


def expired(google_id=EXPIRED_ID, created_ms=EXPIRED_CREATED_MS, expires_ms=EXPIRED_EXPIRES_MS):
    return embedded_export(google_id, created_ms, expires_ms, expired=True)


def in_progress():
    return embedded_export(IN_PROGRESS_ID, IN_PROGRESS_CREATED_MS, None)


def list_page(*exports, rows=None, order=None):
    page = summary(*(rows or (completed_row(), expired_row())))
    return with_blocks(page, list_blocks(list(exports or (completed(), expired()))), order)


def detail_page(*exports, page=None):
    return with_blocks(page or archive(), detail_blocks(list(exports or (completed(), expired()))))


def raw_block(raw, key="ds:1"):
    return callback(key, raw=raw)


def read(page, now=NOW):
    return takeout_data.read_embedded(page, now)


class ExtractionTests(unittest.TestCase):
    def test_the_list_page_is_read_whatever_order_its_blocks_come_in(self):
        for order in itertools.permutations(["ds:0", "ds:1", "ds:2"]):
            with self.subTest(order=order):
                found = read(list_page(order=list(order)))
                self.assertIsNone(found.unused)
                self.assertEqual(set(found.exports), {COMPLETED_ID, EXPIRED_ID})

    def test_a_completed_export_carries_its_times_and_parts(self):
        export = read(list_page()).exports[COMPLETED_ID]
        self.assertEqual((export.created_at, export.expires_at), (CREATED_MS / 1000, EXPIRES_MS / 1000))
        self.assertEqual(export.parts, tuple(takeout_data.EmbeddedPart(index, NAMES[index], size, 0) for index, size in enumerate(SIZES)))

    def test_an_expired_export_has_its_expiry_in_the_other_slot_and_no_parts(self):
        export = read(list_page()).exports[EXPIRED_ID]
        self.assertEqual((export.created_at, export.expires_at, export.parts), (EXPIRED_CREATED_MS / 1000, EXPIRED_EXPIRES_MS / 1000, None))

    def test_the_detail_page_has_one_block(self):
        page = detail_page()
        self.assertEqual(page.count("AF_initDataCallback({key: 'ds:"), 1)
        self.assertEqual(set(read(page).exports), {COMPLETED_ID, EXPIRED_ID})

    def test_googles_own_download_count_is_read_and_the_records_of_each_download_are_not_kept(self):
        parts = [embedded_part(NAMES[0], SIZES[0], 0, 3), embedded_part(NAMES[1], SIZES[1], 1, 0), embedded_part(NAMES[2], SIZES[2], 2, 1)]
        parts[2][4] = {"anything": CANARY}
        export = read(list_page(completed(parts=parts), expired())).exports[COMPLETED_ID]
        self.assertEqual([part.google_downloads for part in export.parts], [3, 0, 1])
        self.assertNotIn(CANARY, repr(export))
        self.assertNotIn("made-up-download", repr(export))

    def test_a_page_without_blocks_says_so(self):
        for page in (summary(completed_row()), archive(), "", "<script>var AF_initDataCallback = 1;</script>"):
            with self.subTest(page=page[:20]):
                self.assertEqual(read(page), takeout_data.Embedded(unused=takeout_data.NONE_ON_THE_PAGE))

    def test_blocks_with_other_keys_or_no_data_are_passed_over(self):
        page = with_blocks(summary(completed_row()), [raw_block(json.dumps([[None, completed()]]), key="ds:x"), raw_block(json.dumps([[None, completed()]]), key="made-up"),
                                                      "<script>AF_initDataCallback({key: 'ds:1', hash: '1'});</script>"])
        self.assertEqual(read(page).unused, takeout_data.NONE_ON_THE_PAGE)

    def test_the_same_export_in_two_blocks_must_say_the_same(self):
        blocks = list_blocks([completed(), expired()])
        blocks["ds:1"] = [[[None, completed(created_ms=CREATED_MS + 1)]]]
        self.assertEqual(read(with_blocks(summary(completed_row()), blocks)).unused, takeout_data.UNCHECKED)

    def test_a_creation_time_in_the_future_is_bounded_by_the_jobs_clock_when_there_is_one(self):
        self.assertEqual(read(list_page(completed(created_ms=int(NOW * 1000) + takeout_data.FUTURE_SLACK_MS - 1, expires_ms=EXPIRES_MS))).unused, None)
        self.assertEqual(read(list_page(completed(created_ms=int(NOW * 1000) + takeout_data.FUTURE_SLACK_MS + 1))).unused, takeout_data.UNCHECKED)
        self.assertIsNone(takeout_data.read_embedded(list_page(completed(created_ms=int(NOW * 1000) + 10 * takeout_data.FUTURE_SLACK_MS))).unused)


def broken(export_change=None, part_change=None):
    export = completed()
    if part_change is not None:
        part_change(export[8])
    if export_change is not None:
        export_change(export)
    return list_page(export, expired())


def set_slot(slot, value):
    def change(container):
        container[slot] = value
    return change


def set_part(slot, value, which=0):
    def change(parts):
        parts[which][slot] = value
    return change


class ValidationTests(unittest.TestCase):
    def assert_refused(self, page, reason):
        found = read(page)
        self.assertEqual((found.unused, found.exports), (reason, {}))

    def test_timestamps_outside_a_plausible_range_are_refused(self):
        cases = {
            "before 2015": set_slot(22, takeout_data.EARLIEST_MS - 1),
            "in the future": set_slot(22, int(NOW * 1000) + 2 * takeout_data.FUTURE_SLACK_MS),
            "seconds not milliseconds": set_slot(22, int(NOW)),
            "a fraction": set_slot(22, CREATED_MS + 0.5),
            "a boolean": set_slot(22, True),
            "text": set_slot(22, str(CREATED_MS)),
            "missing": set_slot(22, None),
            "an expiry before the creation": set_slot(24, CREATED_MS - 1),
            "an expiry at the creation": set_slot(24, CREATED_MS),
            "an expiry months later": set_slot(24, CREATED_MS + takeout_data.LONGEST_LIFE_MS + 1),
            "no expiry for a completed export": set_slot(24, None),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_refused(broken(change), takeout_data.UNCHECKED)

    def test_parts_that_do_not_fit_are_refused(self):
        cases = {
            "a size of zero": set_part(1, 0),
            "a negative size": set_part(1, -5),
            "a size past the limit": set_part(1, takeout_data.MAX_PART_BYTES + 1),
            "a size as text": set_part(1, "39209062486"),
            "a negative download count": set_part(2, -1),
            "a download count past the limit": set_part(2, takeout_data.MAX_GOOGLE_DOWNLOADS + 1),
            "an index past the count": set_part(8, 3),
            "a repeated index": set_part(8, 1),
            "a negative index": set_part(8, -1),
            "a name that is not a zip": set_part(0, "takeout-20261117.tar"),
            "a name with a path": set_part(0, "../takeout-1.zip"),
            "a repeated name": set_part(0, NAMES[1]),
            "a short part": lambda parts: parts[0].pop(),
            "a long part": lambda parts: parts[0].append(None),
            "a part that is not a list": lambda parts: parts.__setitem__(0, {"name": NAMES[0]}),
        }
        for name, change in cases.items():
            with self.subTest(name):
                self.assert_refused(broken(part_change=change), takeout_data.UNCHECKED)
        for name, value in (("an empty part list", []), ("a part list that is not a list", "made-up"),
                            ("too many parts", [embedded_part(f"p-{index}.zip", 1, index) for index in range(takeout_data.MAX_PARTS + 1)])):
            with self.subTest(name):
                self.assert_refused(broken(set_slot(8, value)), takeout_data.UNCHECKED)

    def test_a_row_of_another_arity_is_not_an_export(self):
        for change in (lambda export: export.pop(), lambda export: export.append(None)):
            with self.subTest():
                export = completed()
                change(export)
                self.assert_refused(list_page(export), takeout_data.NONE_ON_THE_PAGE)

    def test_an_id_that_is_not_uuid_shaped_is_not_an_export(self):
        for google_id in ("x" * 36, COMPLETED_ID.upper(), COMPLETED_ID + "0", f"../{COMPLETED_ID}"):
            with self.subTest(google_id=google_id):
                self.assert_refused(list_page(completed(google_id=google_id)), takeout_data.NONE_ON_THE_PAGE)


class HostileInputTests(unittest.TestCase):
    def refusal(self, raw):
        return read(with_blocks(summary(completed_row()), [raw_block(raw)])).unused

    def test_values_that_are_not_strict_json_are_refused(self):
        good = json.dumps([[None, completed()]])
        cases = {
            "single quotes": good.replace('"made-up-ready"', "'made-up-ready'"),
            "a trailing comma": good[:-1] + ",]",
            "NaN": good.replace("null", "NaN", 1),
            "Infinity": good.replace("null", "Infinity", 1),
            "negative infinity": good.replace("null", "-Infinity", 1),
            "a huge number": good.replace(str(CREATED_MS), "9" * 400, 1),
            "a number with too many digits": good.replace(str(CREATED_MS), "1" * (takeout_data.MAX_DIGITS + 1), 1),
            "a float too large": good.replace("null", "1e999", 1),
            "duplicate keys": '[{"a": 1, "a": 2}]',
            "a comment": "[/* made-up */ 1]",
            "a hex escape": '["\\x3c"]',
            "unbalanced": "[[1, 2]",
            "an unterminated string": '["made-up]',
            "an identifier": "[made_up]",
        }
        for name, raw in cases.items():
            with self.subTest(name):
                self.assertEqual(self.refusal(raw), takeout_data.NOT_DATA)

    def test_a_call_that_is_not_plain_data_is_refused(self):
        cases = {
            "a function": "<script>AF_initDataCallback({key: 'ds:1', data: function(){return [1]}});</script>",
            "a call": "<script>AF_initDataCallback({key: 'ds:1', data: made_up([1])});</script>",
            "no object": "<script>AF_initDataCallback([1]);</script>",
            "a repeated field": "<script>AF_initDataCallback({key: 'ds:1', data: [1], data: [2]});</script>",
            "a quoted field name": "<script>AF_initDataCallback({'key': 'ds:1', data: [1]});</script>",
            "no colon": "<script>AF_initDataCallback({key 'ds:1', data: [1]});</script>",
            "no comma": "<script>AF_initDataCallback({key: 'ds:1' data: [1]});</script>",
            "cut short": "<script>AF_initDataCallback({key: 'ds:1', data:</script>",
            "an unterminated key": "<script>AF_initDataCallback({key: 'ds:1, data: [1]});</script>",
        }
        for name, script in cases.items():
            with self.subTest(name):
                self.assertEqual(read(with_blocks(summary(completed_row()), [script])).unused, takeout_data.NOT_DATA)

    def test_a_closing_tag_inside_a_string_ends_the_script_as_a_browser_would(self):
        good = json.dumps([[None, completed()], "made-up </script><script>alert(1)</script>"])
        self.assertEqual(self.refusal(good), takeout_data.NOT_DATA)
        self.assertIsNone(self.refusal(json.dumps([[None, completed()], "made-up <\\/script>"])))

    def test_blocks_past_the_caps_are_refused(self):
        deep = "[" * (takeout_data.MAX_DEPTH + 1) + "]" * (takeout_data.MAX_DEPTH + 1)
        self.assertEqual(self.refusal(deep), takeout_data.TOO_DEEP)
        self.assertEqual(self.refusal("[" * takeout_data.MAX_DEPTH + "]" * takeout_data.MAX_DEPTH), takeout_data.NONE_ON_THE_PAGE)
        with mock.patch.object(takeout_data, "MAX_VALUES", 50):
            self.assertEqual(self.refusal(json.dumps([[None, completed()]])), takeout_data.TOO_MANY)
        with mock.patch.object(takeout_data, "MAX_BLOCK_CHARS", 500):
            self.assertEqual(self.refusal(json.dumps([[None, completed()]])), takeout_data.TOO_LARGE)
        with mock.patch.object(takeout_data, "MAX_SCRIPT_CHARS", 500):
            self.assertEqual(self.refusal(json.dumps([[None, completed()]])), takeout_data.TOO_LARGE)
        with mock.patch.object(takeout_data, "MAX_PAGE_CHARS", 500):
            self.assertEqual(read(list_page()).unused, takeout_data.TOO_LARGE)
        many = [raw_block("[1]", key=f"ds:{number}") for number in range(takeout_data.MAX_BLOCKS + 1)]
        self.assertEqual(read(with_blocks(list_page(), many)).unused, takeout_data.TOO_MANY)

    def test_the_full_size_caps_hold_against_a_real_sized_block(self):
        big = json.dumps([[None, completed()], ["made-up-" + "x" * 1000] * (takeout_data.MAX_BLOCK_CHARS // 1000)])
        self.assertEqual(self.refusal(big), takeout_data.TOO_LARGE)

    def test_a_block_refused_beside_one_that_reads_does_not_stop_the_exports_being_read(self):
        page = with_blocks(list_page(), [raw_block("[" * 40 + "]" * 40, key="ds:2")])
        self.assertEqual(set(read(page).exports), {COMPLETED_ID, EXPIRED_ID})

    def test_every_reason_is_fixed_and_names_nothing_from_the_page(self):
        pages = [summary(completed_row()), list_page(completed(created_ms=1)), with_blocks(summary(), [raw_block(f'["{CANARY}", {CANARY}]')]),
                 with_blocks(summary(), [raw_block("[" * 30 + "]" * 30)])]
        for page in pages:
            with self.subTest():
                reason = read(page).unused
                self.assertIn(reason, FIXED_REASONS)
                for leak in (CANARY, COMPLETED_ID, "takeout-", ".zip"):
                    self.assertNotIn(leak, reason)

    def test_the_part_size_limit_is_the_one_darkroom_accepts(self):
        from darkroom.domain import downloads

        self.assertEqual(takeout_data.MAX_PART_BYTES, downloads.MAX_PART_BYTES)


def listed(page, now=NOW):
    return takeout.read_exports(page, MANAGE_PAGE, now, NEW_YORK)


def dom_only(page):
    return takeout.parse_exports(page.split("<script nonce=\"made-up\">AF_init")[0] + "</body></html>", MANAGE_PAGE, NOW, NEW_YORK)


class SummaryReconcileTests(unittest.TestCase):
    def test_the_embedded_times_are_preferred_when_both_sources_agree(self):
        found = listed(list_page())
        self.assertIsNone(found.unused)
        done, gone = found.listings
        self.assertEqual((done.google_id, done.state, done.created_at, done.expires_at), (COMPLETED_ID, "ready", CREATED_MS / 1000, EXPIRES_MS / 1000))
        self.assertEqual((gone.google_id, gone.state, gone.created_at, gone.expires_at), (EXPIRED_ID, "expired", EXPIRED_CREATED_MS / 1000, EXPIRED_EXPIRES_MS / 1000))
        self.assertEqual([each.archive_url for each in found.listings], [each.archive_url for each in dom_only(list_page())])

    def test_an_export_in_progress_takes_its_embedded_creation_time(self):
        found = listed(list_page(completed(), expired(), in_progress(), rows=(completed_row(), in_progress_row(), expired_row())))
        self.assertIsNone(found.unused)
        self.assertEqual([(each.state, each.created_at, each.expires_at) for each in found.listings],
                         [("ready", CREATED_MS / 1000, EXPIRES_MS / 1000), ("in_progress", IN_PROGRESS_CREATED_MS / 1000, None), ("expired", EXPIRED_CREATED_MS / 1000, EXPIRED_EXPIRES_MS / 1000)])

    def test_a_date_whose_text_cannot_be_read_comes_from_the_embedded_data(self):
        rows = (completed_row(created="Created Novembre 9, 2:45 PM"), expired_row(created="Created yesterday 2:45 PM ago"))
        found = listed(list_page(rows=rows))
        self.assertEqual([(each.created_at, each.expires_at) for each in found.listings], [(CREATED_MS / 1000, EXPIRES_MS / 1000), (EXPIRED_CREATED_MS / 1000, EXPIRED_EXPIRES_MS / 1000)])
        with self.assertRaises(takeout.UnreadableExports):
            dom_only(list_page(rows=rows))

    def test_each_disagreement_falls_back_to_the_page_and_names_the_field(self):
        cases = {
            takeout.ABOUT_ID: list_page(completed(google_id="0a1b2c3d-1111-4aaa-8bbb-0000000000ff"), expired()),
            takeout.ABOUT_COUNT: list_page(embedded_export(COMPLETED_ID, CREATED_MS, None), expired()),
            takeout.ABOUT_CREATED: list_page(completed(created_ms=CREATED_MS - 86_400_000 - 60_000), expired()),
            takeout.ABOUT_EXPIRY: list_page(completed(expires_ms=EXPIRES_MS + 86_400_000 + 60_000), expired()),
        }
        cases["expired with parts"] = list_page(completed(), completed(google_id=EXPIRED_ID))
        for about, page in cases.items():
            with self.subTest(about):
                found = listed(page)
                field = takeout.ABOUT_COUNT if about == "expired with parts" else about
                self.assertEqual(found.unused, f"it and the page disagree about the {field}")
                self.assertEqual(found.listings, dom_only(page))

    def test_an_expiry_the_page_shows_but_the_embedded_data_lacks_is_a_disagreement(self):
        page = list_page(completed(), embedded_export(EXPIRED_ID, EXPIRED_CREATED_MS, None, expired=True),
                         rows=(completed_row(), row(EXPIRED_ID, "Expired", "Created November 9, 2:45 PM", "Available until November 16, 2026")))
        self.assertEqual(listed(page).unused, f"it and the page disagree about the {takeout.ABOUT_EXPIRY}")

    def test_a_disagreement_when_the_page_alone_is_incomplete_is_refused_in_fixed_words(self):
        page = list_page(completed(created_ms=CREATED_MS - 3 * 86_400_000), expired(), rows=(completed_row(), expired_row(created=f"Created {CANARY}")))
        with self.assertRaises(takeout.UnreadableExports) as raised:
            listed(page)
        self.assertEqual(str(raised.exception), "Takeout's embedded data and the page disagree about the creation date")

    def test_embedded_data_that_is_missing_or_refused_leaves_the_page_result_exactly_as_before(self):
        cases = {
            takeout_data.NONE_ON_THE_PAGE: summary(completed_row(), expired_row()),
            takeout_data.NOT_DATA: with_blocks(summary(completed_row(), expired_row()), [raw_block("[1,]")]),
            takeout_data.TOO_DEEP: with_blocks(summary(completed_row(), expired_row()), [raw_block("[" * 30 + "]" * 30)]),
            takeout_data.UNCHECKED: list_page(completed(created_ms=1), expired()),
        }
        for reason, page in cases.items():
            with self.subTest(reason):
                found = listed(page)
                self.assertEqual((found.listings, found.unused), (dom_only(page), reason))

    def test_unreadable_text_with_refused_embedded_data_still_fails_as_before(self):
        page = list_page(completed(created_ms=1), expired(), rows=(completed_row(created="Created Novembre 9"),))
        with self.assertRaises(takeout.UnreadableExports) as raised:
            listed(page)
        self.assertTrue(str(raised.exception).startswith(takeout.DATE_UNREADABLE))

    def test_an_empty_summary_needs_no_embedded_data(self):
        page = summary(body="<div>No requests</div>", scheduled=False)
        self.assertEqual(listed(page), takeout.ReadExports([], None))


def detail(page, needed=None, now=NOW):
    return takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID, needed, now=now)


def archive_dom_only(page, needed=None):
    found = takeout.parse_archive(page.split("<script nonce=\"made-up\">AF_init")[0] + "</body></html>", ARCHIVE_PAGE, COMPLETED_ID, needed)
    return replace(found, unused=None)


def parts_page(*parts, page=None):
    return detail_page(completed(parts=list(parts)), expired(), page=page)


class ArchiveReconcileTests(unittest.TestCase):
    def test_both_sources_agreeing_give_googles_counts_and_the_pages_links(self):
        page = detail_page(completed(parts=embedded_parts(downloaded=(1,))), expired(), page=archive(items=downloaded_page_parts(count=3, sizes=SIZES)))
        found = detail(page)
        self.assertIsNone(found.unused)
        self.assertEqual((found.state, found.created_on, found.expires_on), ("ready", date(2026, 11, 17), date(2026, 11, 24)))
        self.assertEqual([(part.number, part.bytes, part.google_downloads, part.already_downloaded) for part in found.parts],
                         [(1, SIZES[0], 2, True), (2, SIZES[1], 0, False), (3, SIZES[2], 0, False)])
        page_parts = archive_dom_only(page).parts
        self.assertEqual([(part.href, part.link) for part in found.parts], [(part.href, part.link) for part in page_parts])

    def test_the_embedded_size_is_exact_where_the_page_shows_only_a_rounded_one(self):
        items = [part_item(COMPLETED_ID, index, 3, size, NAMES[index], size_attribute=False) for index, size in enumerate(SIZES)]
        found = detail(detail_page(page=archive(items=items)))
        self.assertIsNone(found.unused)
        self.assertEqual([part.bytes for part in found.parts], list(SIZES))

    def test_an_expired_export_has_no_parts_either_way(self):
        page = detail_page(page=archive(status="Expired", created="November 9, 2026", items=[], expires=None))
        found = takeout.parse_archive(page, ARCHIVE_PAGE, EXPIRED_ID, now=NOW)
        self.assertEqual((found.state, found.parts, found.unused, found.expires_on), ("expired", (), None, date(2026, 11, 16)))

    def test_a_date_whose_text_cannot_be_read_comes_from_the_embedded_data(self):
        found = detail(detail_page(page=archive(created="Novembre 17, 2026", expires="24/11/2026")))
        self.assertEqual((found.created_on, found.expires_on, found.unused), (date(2026, 11, 17), date(2026, 11, 24), None))

    def disagreements(self):
        swapped = embedded_parts()
        swapped[0][8], swapped[1][8] = 1, 0
        flagged = embedded_parts(downloaded=(2,))
        return {
            takeout.ABOUT_ID: detail_page(completed(google_id="0a1b2c3d-1111-4aaa-8bbb-0000000000ff"), expired()),
            takeout.ABOUT_COUNT: parts_page(*embedded_parts(sizes=(*SIZES, 1000))),
            takeout.ABOUT_NAMES: parts_page(*embedded_parts(names=["made-up-1.zip", NAMES[1], NAMES[2]])),
            takeout.ABOUT_INDEXES: parts_page(*swapped),
            takeout.ABOUT_SIZES: parts_page(*embedded_parts(sizes=(SIZES[0] + 1, SIZES[1], SIZES[2]))),
            takeout.ABOUT_DOWNLOADED: parts_page(*flagged),
            takeout.ABOUT_CREATED: detail_page(completed(created_ms=CREATED_MS - 3 * 86_400_000), expired()),
            takeout.ABOUT_EXPIRY: detail_page(completed(expires_ms=EXPIRES_MS + 3 * 86_400_000), expired()),
        }

    def test_each_disagreement_falls_back_to_the_complete_page_and_names_the_field(self):
        for about, page in self.disagreements().items():
            with self.subTest(about):
                found = detail(page)
                self.assertEqual(found.unused, f"it and the page disagree about the {about}")
                self.assertEqual(replace(found, unused=None), archive_dom_only(page))
                self.assertTrue(all(part.google_downloads is None for part in found.parts))

    def test_a_completed_page_whose_embedded_export_has_no_parts_disagrees_about_the_count(self):
        page = detail_page(embedded_export(COMPLETED_ID, CREATED_MS, EXPIRES_MS), expired())
        self.assertEqual(detail(page).unused, f"it and the page disagree about the {takeout.ABOUT_COUNT}")

    def test_a_disagreement_with_an_incomplete_page_is_refused_in_fixed_words(self):
        unreadable = part_item(COMPLETED_ID, 0, 3, SIZES[0], NAMES[0], label="", index_attribute=False, downloaded=True)
        items = [unreadable, *[part_item(COMPLETED_ID, index, 3, SIZES[index], NAMES[index]) for index in (1, 2)]]
        for about in (takeout.ABOUT_SIZES, takeout.ABOUT_CREATED):
            changed = embedded_parts(sizes=(SIZES[0], SIZES[1] + 1, SIZES[2])) if about == takeout.ABOUT_SIZES else embedded_parts()
            created = CREATED_MS if about == takeout.ABOUT_SIZES else CREATED_MS - 3 * 86_400_000
            page = detail_page(completed(created_ms=created, parts=changed), expired(), page=archive(items=items))
            with self.subTest(about), self.assertRaises(takeout.UnreadableExports) as raised:
                detail(page, needed=2)
            self.assertEqual(str(raised.exception), f"Takeout's embedded data and the page disagree about the {about}")
        page = detail_page(completed(parts=embedded_parts(sizes=(SIZES[0] + 1, SIZES[1], SIZES[2]))), expired(), page=archive(created=CANARY))
        with self.assertRaises(takeout.UnreadableExports) as raised:
            detail(page)
        self.assertEqual(str(raised.exception), "Takeout's embedded data and the page disagree about the sizes")
        self.assertEqual(detail(detail_page(completed(created_ms=CREATED_MS - 3 * 86_400_000), expired(), page=archive(created=CANARY))).created_on, date(2026, 11, 14))

    def test_the_download_job_still_needs_only_its_part_when_the_sources_agree(self):
        unreadable = part_item(COMPLETED_ID, 0, 3, SIZES[0], NAMES[0], label="", index_attribute=False, downloaded=True)
        items = [unreadable, *[part_item(COMPLETED_ID, index, 3, SIZES[index], NAMES[index]) for index in (1, 2)]]
        found = detail(detail_page(completed(parts=embedded_parts(downloaded=(1,))), expired(), page=archive(items=items)), needed=2)
        self.assertEqual(([part.number for part in found.parts], found.unreadable, found.unused), ([2, 3], 1, None))

    def test_embedded_data_that_is_missing_or_refused_leaves_the_page_result_exactly_as_before(self):
        bad_time = completed(created_ms=takeout_data.EARLIEST_MS - 1)
        short = completed()
        short.pop()
        cases = {
            takeout_data.NONE_ON_THE_PAGE: archive(),
            takeout_data.NOT_DATA: with_blocks(archive(), [raw_block('[[null, {"a": NaN}]]', key="ds:0")]),
            takeout_data.TOO_DEEP: with_blocks(archive(), [raw_block("[" * 30 + "]" * 30, key="ds:0")]),
            takeout_data.UNCHECKED: detail_page(bad_time, expired()),
            "wrong arity": detail_page(short),
        }
        for reason, page in cases.items():
            with self.subTest(reason):
                found = detail(page)
                self.assertEqual(found.unused, takeout_data.NONE_ON_THE_PAGE if reason == "wrong arity" else reason)
                self.assertEqual(replace(found, unused=None), archive_dom_only(page))
        with mock.patch.object(takeout_data, "MAX_BLOCK_CHARS", 500):
            self.assertEqual(detail(detail_page()).unused, takeout_data.TOO_LARGE)

    def test_a_page_the_old_parser_refuses_is_still_refused(self):
        names = part_names(3)
        page = detail_page(page=archive(items=[part_item(COMPLETED_ID, index, 3, size, names[index]) for index, size in enumerate(SIZES)][:2]))
        with self.assertRaises(takeout.UnreadableExports) as raised:
            detail(page)
        self.assertEqual(str(raised.exception), takeout.TOLD_APART)

    def test_messages_name_no_file_names_ids_or_values(self):
        for about, page in self.disagreements().items():
            with self.subTest(about):
                said = detail(page).unused
                for leak in (COMPLETED_ID, "takeout-2026", ".zip", "made-up", str(SIZES[0]), str(CREATED_MS)):
                    self.assertNotIn(leak, said)

    def test_the_download_job_reads_the_same_page_without_a_clock(self):
        found = takeout.parse_archive(detail_page(), ARCHIVE_PAGE, COMPLETED_ID, 1)
        self.assertEqual(([part.google_downloads for part in found.parts], found.unused), ([0, 0, 0], None))


BRISBANE = ZoneInfo("Australia/Brisbane")
LOS_ANGELES = ZoneInfo("America/Los_Angeles")


def ms(*when, zone):
    return int(datetime(*when, tzinfo=zone).timestamp() * 1000)


def zoned(zone, created_ms, expires_ms, created="November 17, 2026", expires=None, now=None):
    now = now if now is not None else created_ms / 1000 + 3600
    export = completed(created_ms=created_ms, expires_ms=expires_ms)
    listing = takeout.parse_exports(summary(completed_row(created="Created 1 hour ago", available=None)), MANAGE_PAGE, now, zone)[0]
    page = detail_page(export, page=archive(created=created, expires=expires))
    found = takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID, now=now, zone=zone)
    return found, (takeout.with_archive(listing, found, zone) if found.unused is None else None)


class ZoneTests(unittest.TestCase):
    def test_an_embedded_expiry_keeps_its_instant_whatever_the_workers_zone(self):
        cases = {
            "ahead of UTC, the UTC date the day before": (BRISBANE, ms(2026, 11, 17, 9, 0, zone=BRISBANE), ms(2026, 11, 24, 8, 0, zone=BRISBANE), date(2026, 11, 24)),
            "behind UTC, the UTC date the day after": (LOS_ANGELES, ms(2026, 11, 17, 9, 0, zone=LOS_ANGELES), ms(2026, 11, 24, 20, 0, zone=LOS_ANGELES), date(2026, 11, 24)),
            "the night daylight saving ends": (NEW_YORK, ms(2026, 10, 26, 12, 0, zone=NEW_YORK), ms(2026, 11, 1, 1, 30, zone=NEW_YORK), date(2026, 11, 1)),
            "late on the day daylight saving ends": (NEW_YORK, ms(2026, 10, 26, 12, 0, zone=NEW_YORK), ms(2026, 11, 1, 23, 30, zone=NEW_YORK), date(2026, 11, 1)),
            "the night daylight saving starts": (NEW_YORK, ms(2026, 3, 2, 12, 0, zone=NEW_YORK), ms(2026, 3, 8, 3, 30, zone=NEW_YORK), date(2026, 3, 8)),
        }
        for name, (zone, created_ms, expires_ms, local_day) in cases.items():
            with self.subTest(name):
                found, merged = zoned(zone, created_ms, expires_ms, created=datetime.fromtimestamp(created_ms / 1000, zone).strftime("%B %d, %Y"))
                self.assertIsNone(found.unused)
                self.assertEqual((found.expires_at, found.expires_on), (expires_ms / 1000, local_day))
                self.assertEqual(merged.expires_at, expires_ms / 1000)

    def test_an_export_is_not_expired_early_or_late_when_the_utc_date_differs(self):
        from darkroom.domain import exports
        from darkroom_browser.jobs import status

        expires_ms = ms(2026, 11, 24, 8, 0, zone=BRISBANE)
        _, merged = zoned(BRISBANE, ms(2026, 11, 17, 9, 0, zone=BRISBANE), expires_ms)
        (item,) = exports.listing_from_result(status.report([merged]))
        self.assertEqual(item.expires_at, expires_ms / 1000)
        self.assertFalse(exports.is_late(item, expires_ms / 1000 - 3600))
        self.assertTrue(exports.is_late(item, expires_ms / 1000))

    def test_the_pages_dates_are_compared_with_the_embedded_times_on_the_workers_calendar(self):
        created_ms = ms(2026, 11, 17, 7, 0, zone=BRISBANE)
        expires_ms = ms(2026, 11, 24, 8, 0, zone=BRISBANE)
        found, _ = zoned(BRISBANE, created_ms, expires_ms, created="November 18, 2026", expires="November 25, 2026")
        self.assertEqual((found.unused, found.created_on, found.expires_on), (None, date(2026, 11, 17), date(2026, 11, 24)))
        found, _ = zoned(BRISBANE, created_ms, expires_ms, created="November 19, 2026")
        self.assertEqual(found.unused, f"it and the page disagree about the {takeout.ABOUT_CREATED}")
        found, _ = zoned(LOS_ANGELES, ms(2026, 11, 17, 20, 0, zone=LOS_ANGELES), ms(2026, 11, 24, 20, 0, zone=LOS_ANGELES), created="November 16, 2026",
                         expires="November 23, 2026")
        self.assertEqual((found.unused, found.created_on, found.expires_on), (None, date(2026, 11, 17), date(2026, 11, 24)))
        found, _ = zoned(LOS_ANGELES, ms(2026, 11, 17, 20, 0, zone=LOS_ANGELES), ms(2026, 11, 24, 20, 0, zone=LOS_ANGELES), expires="November 22, 2026")
        self.assertEqual(found.unused, f"it and the page disagree about the {takeout.ABOUT_EXPIRY}")

    def test_the_list_compares_and_keeps_instants_in_any_zone(self):
        for zone in (BRISBANE, LOS_ANGELES, NEW_YORK):
            with self.subTest(zone=str(zone)):
                now = datetime(2026, 11, 17, 23, 30, tzinfo=zone).timestamp()
                created_ms = int((now - 3600) * 1000) + 30_000
                expires_ms = int(datetime(2026, 11, 24, 23, 30, tzinfo=zone).timestamp() * 1000)
                page = list_page(completed(created_ms=created_ms, expires_ms=expires_ms), rows=(completed_row(available="Available until November 24, 2026 at 11:30 PM"),))
                found = takeout.read_exports(page, MANAGE_PAGE, now, zone)
                self.assertIsNone(found.unused)
                self.assertEqual((found.listings[0].created_at, found.listings[0].expires_at), (created_ms / 1000, expires_ms / 1000))

    def test_a_page_whose_text_dates_cannot_be_read_takes_the_local_dates_and_instant(self):
        expires_ms = ms(2026, 11, 24, 8, 0, zone=BRISBANE)
        found, merged = zoned(BRISBANE, ms(2026, 11, 17, 7, 0, zone=BRISBANE), expires_ms, created="Novembre 17", expires="24/11/2026")
        self.assertEqual((found.created_on, found.expires_on, found.expires_at, merged.expires_at), (date(2026, 11, 17), date(2026, 11, 24), expires_ms / 1000, expires_ms / 1000))

    def test_the_download_job_reading_an_incomplete_page_compares_dates_on_the_workers_calendar(self):
        from darkroom_browser.jobs import download

        unreadable = part_item(COMPLETED_ID, 0, 3, SIZES[0], NAMES[0], label="", index_attribute=False, downloaded=True)
        items = [unreadable, *[part_item(COMPLETED_ID, index, 3, SIZES[index], NAMES[index]) for index in (1, 2)]]
        export = completed(created_ms=ms(2026, 11, 17, 7, 0, zone=BRISBANE), expires_ms=ms(2026, 11, 24, 8, 0, zone=BRISBANE), parts=embedded_parts(downloaded=(1,)))
        page = detail_page(export, page=archive(created="November 18, 2026", items=items))
        self.assertEqual([part.number for part in takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID, 2, zone=BRISBANE).parts], [2, 3])
        with self.assertRaises(takeout.UnreadableExports):
            takeout.parse_archive(page, ARCHIVE_PAGE, COMPLETED_ID, 2, zone=UTC)
        with mock.patch.dict("os.environ", {"TZ": "Australia/Brisbane"}), mock.patch.object(download, "open_archive", return_value=(page, ARCHIVE_PAGE)):
            self.assertEqual(sorted(download.part_links(mock.Mock(), mock.Mock(), COMPLETED_ID, 3, 2)), [2, 3])

    def test_without_embedded_data_the_pages_date_is_read_as_before(self):
        listing = takeout.parse_exports(summary(completed_row(available=None)), MANAGE_PAGE, NOW, NEW_YORK)[0]
        found = takeout.parse_archive(archive(), ARCHIVE_PAGE, COMPLETED_ID, now=NOW, zone=NEW_YORK)
        self.assertEqual((found.expires_at, found.expires_on), (None, date(2026, 11, 24)))
        self.assertEqual(takeout.with_archive(listing, found, NEW_YORK).expires_at, at(2026, 11, 24, 0, 0))


if __name__ == "__main__":
    unittest.main()
