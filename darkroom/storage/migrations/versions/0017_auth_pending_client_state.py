from alembic import op
import sqlalchemy as sa


revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("auth_pending", schema=None) as batch_op:
        batch_op.add_column(sa.Column("client_state", sa.String(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE auth_pending DROP COLUMN client_state")
