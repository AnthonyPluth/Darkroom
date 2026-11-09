from sqlalchemy import Connection, insert

from darkroom.storage.models import GoogleAccount, ManagedAssetVersion, MediaItem, Person, Pixel, PixelCopy

SHA1 = "a" * 40
BIG_BYTES = 5 * 1024 ** 4 + 17
SNAPSHOT = ' {"state":"placed","placed_at":2.125,"always_send":true,"name":"generated Á 雪.heic"} \n'


def seed_pixel_source_parents(connection: Connection) -> None:
    connection.execute(insert(Person), [
        {"id": 1, "name": "Jo", "slug": "jo", "originals_path": "o", "google_only_path": "g", "created": -0.125},
        {"id": 2, "name": "Sam", "slug": "sam", "originals_path": "s", "google_only_path": "t", "created": 1.25},
    ])
    for number, email in ((1, "jo@example.test"), (2, "sam@example.test")):
        connection.execute(insert(GoogleAccount).values(id=number, email=email, default_person_id=number, created=1.0))
        connection.execute(insert(Pixel).values(id=number, name=f"Generated Pixel {number}", google_account_id=number, created=1.0))
        connection.execute(insert(MediaItem).values(id=number, person_id=number, source="phone", path=f"generated-{number}.heic",
                                                  sha1=SHA1 if number == 1 else "b" * 40, bytes=BIG_BYTES, indexed_at=-0.125))
    connection.execute(insert(PixelCopy).values(id=1, pixel_id=1, item_id=1, person_id=1, state="placed", name="generated Á 雪.heic",
                                              sha1=SHA1, bytes=BIG_BYTES, queued_at=-0.125, placed_at=2.125, pruned_at=None,
                                              requeued=3, always_send=True, seen_again_at=1.125))
    connection.execute(insert(PixelCopy).values(id=2, pixel_id=2, item_id=2, person_id=2, state="queued", sha1="b" * 40,
                                              bytes=BIG_BYTES, queued_at=1.125, requeued=0, always_send=False))
    connection.execute(insert(PixelCopy).values(id=3, pixel_id=1, item_id=2, person_id=1, state="queued", sha1="b" * 40,
                                              bytes=BIG_BYTES, queued_at=1.125, requeued=0, always_send=False))
    for number, person_id, owner, asset, checksum, size in ((1, 1, "owner-a", "asset-a", SHA1, BIG_BYTES),
                                                         (2, 2, "owner-b", "asset-b", "b" * 40, BIG_BYTES),
                                                         (3, 1, "owner-a", "asset-a", "c" * 40, BIG_BYTES),
                                                         (4, 1, "owner-a", "asset-d", "d" * 40, None)):
        connection.execute(insert(ManagedAssetVersion).values(id=number, person_id=person_id, owner_id=owner, asset_id=asset,
                                                             sha1=checksum, bytes=size, kind="IMAGE", state="observed" if size is None else "bytes_verified",
                                                             bytes_verified_at=None if size is None else 1.5, metadata_snapshot=" { } ", seen_at=-0.125))
