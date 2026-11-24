from alembic import op
import sqlalchemy as sa


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None

TABLE = "managed_hold_resolutions"
RECEIPT_INDEX = "ix_managed_hold_resolutions_receipt"
OPEN_INDEX = "uq_managed_hold_resolutions_open"
OPEN = "state IN ('recorded', 'applied')"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("hold", sa.String(), nullable=False),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("offset_minutes", sa.Integer(), nullable=True),
        sa.Column("taken_local", sa.String(), nullable=True),
        sa.Column("state", sa.String(), server_default="recorded", nullable=False),
        sa.Column("result", sa.String(), nullable=True),
        sa.Column("previous_metadata", sa.Text(), nullable=True),
        sa.Column("decided_by_person_id", sa.Integer(), nullable=True),
        sa.Column("decided_at", sa.Float(), nullable=False),
        sa.Column("applied_at", sa.Float(), nullable=True),
        sa.Column("undone_at", sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(["receipt_id"], ["managed_import_receipts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["decided_by_person_id"], ["people.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("hold IN ('unknown_offset', 'no_instant', 'unwritable', 'not_enriched', 'not_settled', 'unusable')", name="ck_managed_hold_resolutions_hold"),
        sa.CheckConstraint("state IN ('recorded', 'applied', 'undone', 'superseded')", name="ck_managed_hold_resolutions_state"),
        sa.CheckConstraint("(hold = 'unknown_offset' AND action IN ('offset', 'skip')) OR (hold = 'no_instant' AND action IN ('capture_time', 'skip')) "
                           "OR (hold = 'unwritable' AND action IN ('drop_description', 'skip')) OR (hold = 'not_enriched' AND action IN ('accept', 'record_offset', 'recheck')) "
                           "OR (hold = 'not_settled' AND action IN ('recheck', 'accept')) OR (hold = 'unusable' AND action IN ('recheck', 'skip'))",
                           name="ck_managed_hold_resolutions_action"),
        sa.CheckConstraint("(action IN ('offset', 'capture_time', 'record_offset') AND offset_minutes IS NOT NULL AND offset_minutes >= -720 AND offset_minutes <= 840 "
                           "AND (offset_minutes / 15) * 15 = offset_minutes) OR (action NOT IN ('offset', 'capture_time', 'record_offset') AND offset_minutes IS NULL)",
                           name="ck_managed_hold_resolutions_offset"),
        sa.CheckConstraint("(action = 'capture_time' AND taken_local IS NOT NULL AND length(taken_local) = 19) OR (action != 'capture_time' AND taken_local IS NULL)",
                           name="ck_managed_hold_resolutions_taken"),
        sa.CheckConstraint("(state = 'recorded' AND applied_at IS NULL AND result IS NULL AND undone_at IS NULL) OR "
                           "(state = 'applied' AND applied_at IS NOT NULL AND result IS NOT NULL AND undone_at IS NULL) OR "
                           "(state = 'undone' AND undone_at IS NOT NULL) OR (state = 'superseded' AND undone_at IS NULL)",
                           name="ck_managed_hold_resolutions_state_shape"),
        sa.CheckConstraint("result IS NULL OR result IN ('cleared', 'still_differs', 'still_unusable', 'rechecked') OR (substr(result, 1, 7) = 'rehold:' AND length(result) > 7)",
                           name="ck_managed_hold_resolutions_result"),
    )
    op.create_index(RECEIPT_INDEX, TABLE, ["receipt_id"])
    op.create_index(OPEN_INDEX, TABLE, ["receipt_id"], unique=True, sqlite_where=sa.text(OPEN), postgresql_where=sa.text(OPEN))


def downgrade() -> None:
    connection = op.get_bind()
    if TABLE not in sa.inspect(connection).get_table_names():
        return
    resolutions = sa.table(TABLE, sa.column("id"))
    if connection.execute(sa.select(resolutions.c.id).limit(1)).first() is not None:
        raise RuntimeError("the owner's hold resolutions are kept; roll the schema forward instead of downgrading past 0062")
    op.drop_index(OPEN_INDEX, table_name=TABLE)
    op.drop_index(RECEIPT_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
