from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sub', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.drop_column('sub')
