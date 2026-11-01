import logging
import sys

from darkroom import monitoring


def shout(count):
    # ruleid: darkroom-log-through-monitoring
    print("synced", count)


def module_logger(count):
    # ruleid: darkroom-log-through-monitoring
    logging.info("synced %s", count)


def named_logger(count):
    # ruleid: darkroom-log-through-monitoring
    logging.getLogger(__name__).warning("synced %s", count)


def raw_stderr(count):
    # ruleid: darkroom-log-through-monitoring
    sys.stderr.write(f"synced {count}\n")


def through_monitoring(count):
    # ok: darkroom-log-through-monitoring
    monitoring.log("synced", count=count)
