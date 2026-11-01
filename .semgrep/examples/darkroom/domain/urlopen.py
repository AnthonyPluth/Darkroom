import urllib.request


def fetch(url):
    # ruleid: darkroom-raw-urlopen
    return urllib.request.urlopen(url)


def fetch_with_opener(url):
    # ruleid: darkroom-raw-urlopen
    return urllib.request.build_opener().open(url)


def request_object(url):
    # ok: darkroom-raw-urlopen
    return urllib.request.Request(url)
