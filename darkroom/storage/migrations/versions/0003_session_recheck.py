from alembic import op
import sqlalchemy as sa


revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.alter_column('expires', existing_type=sa.Float(), nullable=True)
        batch_op.add_column(sa.Column('refresh_token_sealed', sa.Text(), nullable=True))
        batch_op.add_column(sa.Column('last_checked', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('cookie_renewed', sa.Float(), nullable=True))


def downgrade() -> None:
    op.execute("DELETE FROM auth_sessions WHERE expires IS NULL")
    with op.batch_alter_table('auth_sessions', schema=None) as batch_op:
        batch_op.drop_column('cookie_renewed')
        batch_op.drop_column('last_checked')
        batch_op.drop_column('refresh_token_sealed')
        batch_op.alter_column('expires', existing_type=sa.Float(), nullable=False)
