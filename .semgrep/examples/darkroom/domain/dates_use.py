import datetime
from datetime import date


def old_utc():
    # ruleid: darkroom-no-utcnow
    return datetime.datetime.utcnow()


def bare_day():
    # ruleid: darkroom-no-bare-today
    return date.today()


def bare_day_qualified():
    # ruleid: darkroom-no-bare-today
    return datetime.date.today()


def aware_now():
    # ok: darkroom-no-utcnow
    return datetime.datetime.now(datetime.UTC)


def given_day(today):
    # ok: darkroom-no-bare-today
    return today
