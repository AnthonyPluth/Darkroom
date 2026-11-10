from alembic import op
import sqlalchemy as sa


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "index_skips",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("bytes", sa.Integer().with_variant(sa.BigInteger(), "postgresql"), nullable=False),
        sa.Column("mtime", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("skipped_at", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["person_id"], ["people.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_id", "path", name="uq_index_skips_person_path"),
    )


def downgrade() -> None:
    if "index_skips" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("index_skips")
