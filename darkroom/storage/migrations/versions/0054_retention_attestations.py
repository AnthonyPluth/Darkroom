from alembic import op
import sqlalchemy as sa


revision = "0054"
down_revision = "0051"
branch_labels = None
depends_on = None

TABLE = "retention_attestations"
ACTIVE = "withdrawn_at IS NULL"
SHAPE = ("(kind = 'restore_rehearsal' AND rehearsed_on IS NOT NULL AND immich_version IS NOT NULL AND darkroom_version IS NOT NULL AND database_kind IS NOT NULL) OR "
         "(kind = 'remote_deletion_boundary' AND rehearsed_on IS NULL AND immich_version IS NULL AND darkroom_version IS NULL AND database_kind IS NULL)")


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("recorded_at", sa.Float(), nullable=False),
        sa.Column("evidence_url", sa.String(), nullable=False),
        sa.Column("rehearsed_on", sa.String(), nullable=True),
        sa.Column("immich_version", sa.String(), nullable=True),
        sa.Column("darkroom_version", sa.String(), nullable=True),
        sa.Column("database_kind", sa.String(), nullable=True),
        sa.Column("withdrawn_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("kind IN ('restore_rehearsal', 'remote_deletion_boundary')", name="ck_retention_attestations_kind"),
        sa.CheckConstraint("database_kind IN ('sqlite', 'postgresql')", name="ck_retention_attestations_database_kind"),
        sa.CheckConstraint(SHAPE, name="ck_retention_attestations_shape"),
        sa.CheckConstraint("length(evidence_url) > 0", name="ck_retention_attestations_evidence"),
    )
    op.create_index("uq_retention_attestations_active_kind", TABLE, ["kind"], unique=True, sqlite_where=sa.text(ACTIVE), postgresql_where=sa.text(ACTIVE))


def downgrade() -> None:
    connection = op.get_bind()
    attestations = sa.table(TABLE, sa.column("id"))
    if connection.execute(sa.select(attestations.c.id).limit(1)).first() is not None:
        raise RuntimeError("the owner's retention records are kept; roll the schema forward instead of downgrading past 0054")
    op.drop_index("uq_retention_attestations_active_kind", table_name=TABLE)
    op.drop_table(TABLE)
