import datetime
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIST = ROOT / ".trivyignore.yaml"
MAX_DAYS = 180
GLOB = re.compile(r"[*?\[\]{}]")
KEYS = {"id", "paths", "purls", "statement", "expired_at"}


class Refused(Exception):
    pass


def entries(text):
    found = []
    section = None
    current = None
    listing = None
    for number, raw in enumerate(text.splitlines(), 1):
        line = "" if raw.lstrip().startswith("#") else raw.rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            if line != "vulnerabilities:":
                raise Refused(f"line {number}: only vulnerabilities: is accepted")
            section = "vulnerabilities"
            continue
        if section is None:
            raise Refused(f"line {number}: outside vulnerabilities:")
        match = re.fullmatch(r"  - (\w+): (.+)", line)
        if match:
            current = {match.group(1): match.group(2)}
            found.append(current)
            listing = None
            continue
        match = re.fullmatch(r"    (\w+):( (.+))?", line)
        if match and current is not None:
            key, value = match.group(1), match.group(3)
            if value is None:
                listing = current.setdefault(key, [])
            else:
                current[key] = value
                listing = None
            continue
        match = re.fullmatch(r"      - (.+)", line)
        if match and listing is not None:
            listing.append(match.group(1))
            continue
        raise Refused(f"line {number}: not plain block YAML")
    return found


def unquoted(value):
    return value[1:-1] if len(value) >= 2 and value[0] == value[-1] == '"' else value


def check(text, today):
    found = entries(text)
    for entry in found:
        if set(entry) - KEYS:
            raise Refused(f"unknown keys {sorted(set(entry) - KEYS)}")
        for key in ("id", "paths", "statement", "expired_at"):
            if key not in entry:
                raise Refused(f"an entry needs {key}")
        paths = entry["paths"]
        if not isinstance(paths, list) or len(paths) != 1:
            raise Refused("an entry is limited to exactly one path")
        path = unquoted(paths[0])
        if GLOB.search(path) or path.startswith("/") or not path:
            raise Refused(f"{path} is not one exact path")
        for purl in entry.get("purls", []):
            if not re.fullmatch(r"pkg:[a-z]+/[^\s?#@]+(@\S+)?", unquoted(purl)):
                raise Refused(f"{purl} is not a plain purl")
        statement = unquoted(entry["statement"]).strip()
        if not statement:
            raise Refused("an entry needs a reason")
        if statement[0] in "|>":
            raise Refused("the reason is one line")
        if not re.search(r"#\d+", statement):
            raise Refused("the reason names the issue where the owner accepted it")
        expires = datetime.date.fromisoformat(entry["expired_at"])
        if (expires - today).days > MAX_DAYS:
            raise Refused(f"{entry['expired_at']} is more than {MAX_DAYS} days away")
    return found


GOOD = """vulnerabilities:
  - id: CVE-2026-0001
    paths:
      - usr/local/bin/example
    statement: "Not linked in (#1)."
    expired_at: 2027-01-31
"""


class TrivyIgnoreListTests(unittest.TestCase):
    def test_the_list_keeps_to_the_limits_security_md_sets(self):
        today = datetime.datetime.now(datetime.UTC).date()
        found = check(LIST.read_text(), today)
        self.assertTrue(found)

    def test_the_accepted_syncthing_finding_is_limited_to_its_binary(self):
        found = check(LIST.read_text(), datetime.date(2026, 11, 16))
        syncthing = [entry for entry in found if entry["id"] == "CVE-2026-78661"]
        self.assertEqual(len(syncthing), 1)
        self.assertEqual(syncthing[0]["paths"], ["usr/local/bin/syncthing"])
        self.assertEqual(syncthing[0]["purls"], ["pkg:golang/golang.org/x/net"])

    def test_the_accepted_go_stdlib_finding_is_limited_to_the_syncthing_binary(self):
        found = check(LIST.read_text(), datetime.date(2026, 11, 19))
        stdlib = [entry for entry in found if entry["id"] == "CVE-2026-97054"]
        self.assertEqual(len(stdlib), 1)
        self.assertEqual(stdlib[0]["paths"], ["usr/local/bin/syncthing"])
        self.assertEqual(stdlib[0]["purls"], ["pkg:golang/stdlib"])

    def test_the_list_is_owned_and_gets_the_second_review(self):
        owners = (ROOT / ".github/CODEOWNERS").read_text().splitlines()
        self.assertIn("/.trivyignore.yaml @AnthonyPluth", owners)
        review = (ROOT / ".github/workflows/agent-review.yml").read_text()
        paths = review[review.index("      second-review-paths: |\n"):].split("\n    secrets:", 1)[0]
        self.assertIn("\n        .trivyignore.yaml\n", paths)

    def test_a_well_formed_entry_passes(self):
        self.assertEqual(len(check(GOOD, datetime.date(2026, 11, 16))), 1)

    def test_each_limit_refuses_an_entry_that_breaks_it(self):
        today = datetime.date(2026, 11, 16)
        broken = {
            "no id": GOOD.replace("  - id: CVE-2026-0001\n    paths:", "  - paths:"),
            "no paths": GOOD.replace("    paths:\n      - usr/local/bin/example\n", ""),
            "a wildcard": GOOD.replace("usr/local/bin/example", "usr/local/bin/*"),
            "a double wildcard": GOOD.replace("usr/local/bin/example", "**"),
            "two paths": GOOD.replace("      - usr/local/bin/example\n", "      - usr/local/bin/example\n      - usr/bin/other\n"),
            "a leading slash": GOOD.replace("usr/local/bin/example", "/usr/local/bin/example"),
            "no statement": GOOD.replace('    statement: "Not linked in (#1)."\n', ""),
            "no owner's acceptance": GOOD.replace('"Not linked in (#1)."', '"Not linked in."'),
            "an empty statement": GOOD.replace('"Not linked in (#1)."', '""'),
            "a multi-line statement": GOOD.replace('"Not linked in (#1)."', "|"),
            "no expiry": GOOD.replace("    expired_at: 2027-01-31\n", ""),
            "an expiry too far away": GOOD.replace("2027-01-31", "2027-05-16"),
            "a misconfiguration": GOOD + "misconfigurations:\n  - id: AVD-DS-0002\n",
            "an unknown key": GOOD.replace("    expired_at:", "    severity: HIGH\n    expired_at:"),
            "a purl with qualifiers": GOOD.replace("    statement:", "    purls:\n      - pkg:golang/x?arch=amd64\n    statement:"),
        }
        for name, text in broken.items():
            with self.subTest(name), self.assertRaises((Refused, ValueError)):
                check(text, today)

    def test_an_expiry_exactly_180_days_away_passes(self):
        self.assertEqual(len(check(GOOD.replace("2027-01-31", "2027-05-15"), datetime.date(2026, 11, 16))), 1)
