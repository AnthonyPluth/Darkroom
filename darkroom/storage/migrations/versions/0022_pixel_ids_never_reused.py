from alembic import op


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

CHILDREN = ("pixel_people", "pixel_copies")


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
    rebuild_pixels(True)
    op.execute(
        "UPDATE sqlite_sequence SET seq = MAX(seq, (SELECT COALESCE(MAX(CAST(subject AS INTEGER)), 0) FROM events "
        "WHERE type IN ('pixel_created', 'pixel_deleted'))) WHERE name = 'pixels'"
    )
    op.execute(
        "INSERT INTO sqlite_sequence (name, seq) SELECT 'pixels', COALESCE(MAX(CAST(subject AS INTEGER)), 0) FROM events "
        "WHERE type IN ('pixel_created', 'pixel_deleted') AND NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name = 'pixels')"
    )


def downgrade() -> None:
    rebuild_pixels(False)
