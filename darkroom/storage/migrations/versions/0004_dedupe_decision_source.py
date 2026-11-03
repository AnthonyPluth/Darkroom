from alembic import op
import sqlalchemy as sa


revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('dedupe_decisions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source_path', sa.String(), nullable=True))
        batch_op.add_column(sa.Column('person_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key('fk_dedupe_decisions_person_id_people', 'people', ['person_id'], ['id'], ondelete='SET NULL')
        batch_op.create_unique_constraint('uq_dedupe_decisions_export_source', ['export_id', 'source_path'])


def downgrade() -> None:
    with op.batch_alter_table('dedupe_decisions', schema=None) as batch_op:
        batch_op.drop_constraint('uq_dedupe_decisions_export_source', type_='unique')
        batch_op.drop_constraint('fk_dedupe_decisions_person_id_people', type_='foreignkey')
        batch_op.drop_column('person_id')
        batch_op.drop_column('source_path')
