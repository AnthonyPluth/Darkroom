from alembic import op
import sqlalchemy as sa


revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('exports', schema=None) as batch_op:
        batch_op.add_column(sa.Column('download_tries', sa.Integer(), server_default='0', nullable=False))
    with op.batch_alter_table('export_parts', schema=None) as batch_op:
        batch_op.add_column(sa.Column('bytes_done', sa.Integer(), nullable=True))


def downgrade() -> None:
    op.execute("ALTER TABLE export_parts DROP COLUMN bytes_done")
    op.execute("ALTER TABLE exports DROP COLUMN download_tries")
