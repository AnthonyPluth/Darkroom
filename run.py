from __future__ import annotations

import argparse
import sys

from darkroom import config, monitoring
from darkroom.providers import exiftool
from darkroom.server import handler


def check_config() -> int:
    settings = config.load()
    found = config.problems(settings)
    try:
        exiftool.version()
    except (exiftool.Missing, OSError) as error:
        found.append(str(error))
    for line in found:
        print(f"config: {line}", file=sys.stderr)
    if not found:
        print("Configuration looks complete.")
    return 1 if found else 0


def serve() -> int:
    settings = config.load()
    server = handler.make_server(settings)
    monitoring.log("serving", port=server.server_address[1])
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.server_close()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="run.py")
    parser.add_argument("command", choices=["serve", "check-config"])
    args = parser.parse_args(argv)
    return serve() if args.command == "serve" else check_config()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
