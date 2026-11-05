from alembic import op
import sqlalchemy as sa


revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('review_items', schema=None) as batch_op:
        batch_op.add_column(sa.Column('sidecar_json', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('hold_sha1', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('review_items', schema=None) as batch_op:
        batch_op.drop_column('hold_sha1')
        batch_op.drop_column('sidecar_json')
