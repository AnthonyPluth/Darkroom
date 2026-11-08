from alembic import op


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

CHILDREN = ("pixel_people", "pixel_copies")
SEQUENCE_FOR_PIXELS = (
    "SELECT setval(sequence, GREATEST(highest, 1), highest >= 1) FROM ("
    "SELECT pg_get_serial_sequence('pixels', 'id') AS sequence, GREATEST("
    "COALESCE(pg_sequence_last_value(pg_get_serial_sequence('pixels', 'id')::regclass), 0), "
    "(SELECT COALESCE(MAX(id), 0) FROM pixels), "
    "(SELECT COALESCE(MAX(CAST(subject AS INTEGER)), 0) FROM events WHERE type IN ('pixel_created', 'pixel_deleted'))) AS highest) AS found"
)


def rebuild_pixels(autoincrement: bool) -> None:
    for child in CHILDREN:
        op.execute(f"CREATE TABLE _kept_{child} AS SELECT * FROM {child}")
        op.execute(f"DELETE FROM {child}")
    with op.batch_alter_table("pixels", schema=None, recreate="always", table_kwargs={"sqlite_autoincrement": autoincrement}):
        pass
    for child in CHILDREN:
        op.execute(f"INSERT INTO {child} SELECT * FROM _kept_{child}")
        op.execute(f"DROP TABLE _kept_{child}")


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(SEQUENCE_FOR_PIXELS)
        return
    rebuild_pixels(True)
    op.execute("INSERT INTO sqlite_sequence (name, seq) SELECT 'pixels', 0 WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'pixels')")
    op.execute(
        "UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT COALESCE(MAX(id), 0) FROM pixels), (SELECT COALESCE(MAX(CAST(subject AS INTEGER)), 0) FROM events "
        "WHERE type IN ('pixel_created', 'pixel_deleted'))) WHERE name = 'pixels'"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        return
    rebuild_pixels(False)
