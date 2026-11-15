from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "stored_settings",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("value_sealed", sa.Text(), nullable=True),
        sa.Column("updated", sa.Float(), nullable=False),
        sa.CheckConstraint("(value IS NULL) <> (value_sealed IS NULL)", name="ck_stored_settings_one_value"),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    if "stored_settings" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("stored_settings")
