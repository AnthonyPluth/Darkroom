from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('dhash', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.drop_column('dhash')
