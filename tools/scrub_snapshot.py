from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from darkroom_browser.scrub import scrub_html


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write a scrubbed copy of a saved page snapshot: scripts, form values, token-like attributes and query strings removed.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    args = parser.parse_args(argv)
    destination = args.destination or args.source.with_name(args.source.stem + ".scrubbed.html")
    destination.write_text(scrub_html(args.source.read_text(encoding="utf-8", errors="replace")), encoding="utf-8")
    print(f"wrote {destination}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
