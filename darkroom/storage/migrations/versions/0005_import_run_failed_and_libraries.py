from alembic import op
import sqlalchemy as sa


revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('import_runs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('failed', sa.Integer(), server_default='0', nullable=False))
        batch_op.add_column(sa.Column('changed_people', sa.Text(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('import_runs', schema=None) as batch_op:
        batch_op.drop_column('changed_people')
        batch_op.drop_column('failed')
