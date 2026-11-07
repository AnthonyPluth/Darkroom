from alembic import op
import sqlalchemy as sa


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "duplicate_removals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("decision_id", sa.Integer(), nullable=True),
        sa.Column("kept_item_id", sa.Integer(), nullable=True),
        sa.Column("removed_item_id", sa.Integer(), nullable=True),
        sa.Column("kept_path", sa.String(), nullable=False),
        sa.Column("kept_sha1", sa.String(), nullable=False),
        sa.Column("removed_path", sa.String(), nullable=False),
        sa.Column("removed_sha1", sa.String(), nullable=False),
        sa.Column("removed_source", sa.String(), nullable=False),
        sa.Column("rule", sa.String(), nullable=False),
        sa.Column("tier", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("removed_at", sa.Float(), nullable=True),
        sa.CheckConstraint("rule IN ('identical', 'phone_better', 'takeout_better')", name="ck_duplicate_removals_rule"),
        sa.CheckConstraint("state IN ('pending', 'removed', 'refused')", name="ck_duplicate_removals_state"),
        sa.ForeignKeyConstraint(["decision_id"], ["dedupe_decisions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["kept_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["removed_item_id"], ["media_items.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_duplicate_removals_person_state", "duplicate_removals", ["person_id", "state"])


def downgrade() -> None:
    op.drop_index("ix_duplicate_removals_person_state", table_name="duplicate_removals")
    op.drop_table("duplicate_removals")
