from alembic import op
import sqlalchemy as sa


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('devices', schema=None) as batch_op:
        batch_op.add_column(sa.Column('push_token_sealed', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('push_token_digest', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('push_token_at', sa.Float(), nullable=True))
    op.create_index('ix_devices_push_token_digest', 'devices', ['push_token_digest'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_devices_push_token_digest', table_name='devices')
    op.execute("ALTER TABLE devices DROP COLUMN push_token_at")
    op.execute("ALTER TABLE devices DROP COLUMN push_token_digest")
    op.execute("ALTER TABLE devices DROP COLUMN push_token_sealed")
