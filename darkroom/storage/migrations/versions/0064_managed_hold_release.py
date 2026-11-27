from alembic import op
import sqlalchemy as sa


revision = "0064"
down_revision = "0063"
branch_labels = None
depends_on = None


def begin_sqlite_ddl(connection: sa.Connection) -> None:
    if connection.dialect.name != "sqlite":
        return
    driver = connection.connection.driver_connection
    assert driver is not None
    if not driver.in_transaction:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def upgrade() -> None:
    connection = op.get_bind()
    begin_sqlite_ddl(connection)
    if connection.dialect.name == "sqlite":
        op.execute("ALTER TABLE review_items ADD COLUMN hold_free_state VARCHAR")
        op.execute("ALTER TABLE review_items ADD COLUMN hold_release_point_id INTEGER REFERENCES recovery_points(id)")
    else:
        op.add_column("review_items", sa.Column("hold_free_state", sa.String(), nullable=True))
        op.add_column("review_items", sa.Column("hold_release_point_id", sa.Integer(), nullable=True))
        op.create_foreign_key("fk_review_items_hold_release_point_id", "review_items", "recovery_points", ["hold_release_point_id"], ["id"])


def downgrade() -> None:
    connection = op.get_bind()
    begin_sqlite_ddl(connection)
    review = sa.table("review_items", sa.column("hold_free_state"), sa.column("hold_release_point_id"))
    if connection.execute(sa.select(review.c.hold_free_state).where(review.c.hold_free_state.is_not(None) | review.c.hold_release_point_id.is_not(None)).limit(1)).first() is not None:
        raise RuntimeError("managed review hold journals are kept; roll the schema forward instead of downgrading past 0064")
    if connection.dialect.name == "sqlite":
        op.execute("ALTER TABLE review_items DROP COLUMN hold_release_point_id")
        op.execute("ALTER TABLE review_items DROP COLUMN hold_free_state")
    else:
        op.drop_constraint("fk_review_items_hold_release_point_id", "review_items", type_="foreignkey")
        op.drop_column("review_items", "hold_release_point_id")
        op.drop_column("review_items", "hold_free_state")
