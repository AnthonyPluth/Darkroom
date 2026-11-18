from alembic import op
import sqlalchemy as sa


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None

NAME = "ck_media_items_time_source"
BEFORE = "'exif_offset', 'gps_lookup', 'sidecar', 'unknown', 'quicktime_utc'"
AFTER = "'exif_offset', 'gps_lookup', 'sidecar', 'unknown', 'quicktime_utc', 'sidecar_offset'"


def allow(old: str, new: str) -> None:
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(f"ALTER TABLE media_items DROP CONSTRAINT {NAME}")
        op.execute(f"ALTER TABLE media_items ADD CONSTRAINT {NAME} CHECK (time_source IN ({new}))")
        return
    table = connection.execute(sa.text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'media_items'")).scalar_one()
    found, wanted = f"time_source IN ({old})", f"time_source IN ({new})"
    if table.count(found) != 1:
        raise RuntimeError("media_items has an unexpected time_source check")
    version = connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
    connection.exec_driver_sql("PRAGMA writable_schema = ON")
    connection.execute(sa.text("UPDATE sqlite_master SET sql = :sql WHERE type = 'table' AND name = 'media_items'"), {"sql": table.replace(found, wanted)})
    connection.exec_driver_sql(f"PRAGMA schema_version = {int(version) + 1}")
    connection.exec_driver_sql("PRAGMA writable_schema = OFF")
    if connection.exec_driver_sql("PRAGMA integrity_check").scalar_one() != "ok":
        raise RuntimeError("media_items failed its integrity check after the time_source change")


def upgrade() -> None:
    allow(BEFORE, AFTER)


def downgrade() -> None:
    op.execute("UPDATE media_items SET time_source = 'sidecar' WHERE time_source = 'sidecar_offset'")
    allow(AFTER, BEFORE)
