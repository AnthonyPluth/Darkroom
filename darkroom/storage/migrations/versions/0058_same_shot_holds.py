from alembic import op
import sqlalchemy as sa


revision = "0058"
down_revision = "0054"
branch_labels = None
depends_on = None

TABLE = "managed_same_shot_holds"
INDEX = "ix_managed_same_shot_holds_person_status"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("receipt_id", sa.Integer(), nullable=False),
        sa.Column("found_at", sa.Float(), nullable=False),
        sa.Column("candidates", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("resolved_at", sa.Float(), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("asset_id", sa.String(), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["receipt_id"], ["managed_import_receipts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["resolved_by"], ["people.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("receipt_id", name="uq_managed_same_shot_holds_receipt"),
        sa.CheckConstraint("status IN ('pending', 'upload', 'represented', 'void')", name="ck_managed_same_shot_holds_status"),
        sa.CheckConstraint("length(owner_id) > 0", name="ck_managed_same_shot_holds_owner"),
        sa.CheckConstraint("(status = 'pending' AND resolved_at IS NULL AND resolved_by IS NULL AND asset_id IS NULL) OR "
                           "(status != 'pending' AND resolved_at IS NOT NULL)", name="ck_managed_same_shot_holds_resolution"),
        sa.CheckConstraint("asset_id IS NULL OR (status = 'represented' AND length(asset_id) > 0)", name="ck_managed_same_shot_holds_asset"),
    )
    op.create_index(INDEX, TABLE, ["person_id", "status"])


def downgrade() -> None:
    connection = op.get_bind()
    if TABLE not in sa.inspect(connection).get_table_names():
        return
    holds = sa.table(TABLE, sa.column("id"))
    if connection.execute(sa.select(holds.c.id).limit(1)).first() is not None:
        raise RuntimeError("the owner's same-shot holds are kept; roll the schema forward instead of downgrading past 0058")
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
