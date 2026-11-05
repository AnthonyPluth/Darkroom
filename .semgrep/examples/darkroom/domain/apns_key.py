from pathlib import Path

from cryptography.hazmat.primitives.serialization import load_pem_private_key

from darkroom.providers import apns


def read_the_key_here(settings):
    # ruleid: darkroom-apns-key-one-reader
    return Path(settings.apns_key_file).read_bytes()


def parse_the_key_here(raw):
    # ruleid: darkroom-apns-key-one-reader
    return load_pem_private_key(raw, password=None)


def push_through_the_provider(settings, clock, tokens, push):
    # ok: darkroom-apns-key-one-reader
    client = apns.of(settings, clock)
    return client.send(tokens, push) if client else []
