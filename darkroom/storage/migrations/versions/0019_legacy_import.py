from alembic import op
import sqlalchemy as sa


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("people", schema=None) as batch_op:
        batch_op.add_column(sa.Column("legacy_path", sa.String(), nullable=True))
    op.create_table(
        "legacy_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("sha1", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=True),
        sa.Column("matched_item_id", sa.Integer(), nullable=True),
        sa.Column("original_item_id", sa.Integer(), nullable=True),
        sa.Column("seen_at", sa.Float(), nullable=False),
        sa.Column("queued_at", sa.Float(), nullable=True),
        sa.CheckConstraint("status IN ('matched', 'unmatched', 'failed')", name="ck_legacy_files_status"),
        sa.CheckConstraint("tier IS NULL OR tier BETWEEN 1 AND 5", name="ck_legacy_files_tier"),
        sa.ForeignKeyConstraint(["matched_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["original_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "path", name="uq_legacy_files_person_path"),
    )


def downgrade() -> None:
    op.drop_table("legacy_files")
    op.execute("ALTER TABLE people DROP COLUMN legacy_path")
