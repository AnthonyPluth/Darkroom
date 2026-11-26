from alembic import op
import sqlalchemy as sa


revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None
JOURNALS = (("export_parts", "release_point_id"), ("takeout_entries", "held_release_point_id"))


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
    for table, column in JOURNALS:
        if connection.dialect.name == "sqlite":
            op.execute(f"ALTER TABLE {table} ADD COLUMN {column} INTEGER REFERENCES recovery_points(id)")
        else:
            op.add_column(table, sa.Column(column, sa.Integer(), nullable=True))
            op.create_foreign_key(f"fk_{table}_{column}", table, "recovery_points", [column], ["id"])


def downgrade() -> None:
    connection = op.get_bind()
    begin_sqlite_ddl(connection)
    for table, column in JOURNALS:
        journal = sa.table(table, sa.column(column))
        if connection.execute(sa.select(journal.c[column]).where(journal.c[column].is_not(None)).limit(1)).first() is not None:
            raise RuntimeError("managed release journals are kept; roll the schema forward instead of downgrading past 0063")
    for table, column in reversed(JOURNALS):
        if connection.dialect.name == "sqlite":
            op.execute(f"ALTER TABLE {table} DROP COLUMN {column}")
        else:
            op.drop_constraint(f"fk_{table}_{column}", table, type_="foreignkey")
            op.drop_column(table, column)
