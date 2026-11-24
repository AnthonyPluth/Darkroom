from alembic import op
import sqlalchemy as sa


revision = "0056"
down_revision = "0054"
branch_labels = None
depends_on = None

POINTS = "recovery_points"
ASSETS = "recovery_point_assets"
SHAPE = ("(state = 'open' AND finished_at IS NULL AND taken_at IS NULL AND kept_until IS NULL AND scopes IS NULL AND failure IS NULL) OR "
         "(state = 'succeeded' AND finished_at IS NOT NULL AND taken_at IS NOT NULL AND kept_until IS NOT NULL AND scopes IS NOT NULL AND failure IS NULL) OR "
         "(state IN ('failed', 'abandoned') AND failure IS NOT NULL)")
TIMES = "taken_at IS NULL OR (finished_at IS NOT NULL AND opened_at <= taken_at AND taken_at <= finished_at)"
FAILURES = "failure IS NULL OR failure IN ('reported_failed', 'scopes_missing', 'not_finished', 'immich_unreachable')"


def upgrade() -> None:
    op.create_table(
        POINTS,
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("opened_at", sa.Float(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("manifest_count", sa.Integer(), nullable=False),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=False),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.Column("taken_at", sa.Float(), nullable=True),
        sa.Column("kept_until", sa.Float(), nullable=True),
        sa.Column("scopes", sa.String(), nullable=True),
        sa.Column("tool", sa.String(length=32), nullable=True),
        sa.Column("snapshot_ref", sa.String(length=128), nullable=True),
        sa.Column("failure", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("state IN ('open', 'succeeded', 'failed', 'abandoned')", name="ck_recovery_points_state"),
        sa.CheckConstraint(SHAPE, name="ck_recovery_points_shape"),
        sa.CheckConstraint(TIMES, name="ck_recovery_points_times"),
        sa.CheckConstraint(FAILURES, name="ck_recovery_points_failure"),
        sa.CheckConstraint("manifest_count >= 0", name="ck_recovery_points_manifest_count"),
        sa.CheckConstraint("length(manifest_sha256) = 64 AND ltrim(manifest_sha256, '0123456789abcdef') = ''", name="ck_recovery_points_manifest_digest"),
    )
    op.create_index("ix_recovery_points_state_opened", POINTS, ["state", "opened_at"])
    op.create_table(
        ASSETS,
        sa.Column("recovery_point_id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.String(), nullable=False),
        sa.Column("asset_id", sa.String(), nullable=False),
        sa.Column("sha1", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("recovery_point_id", "owner_id", "asset_id"),
        sa.ForeignKeyConstraint(["recovery_point_id"], [f"{POINTS}.id"], ondelete="CASCADE"),
        sa.CheckConstraint("source IN ('receipt', 'version')", name="ck_recovery_point_assets_source"),
        sa.CheckConstraint("length(owner_id) > 0 AND length(asset_id) > 0", name="ck_recovery_point_assets_identity"),
        sa.CheckConstraint("length(sha1) = 40 AND ltrim(sha1, '0123456789abcdef') = ''", name="ck_recovery_point_assets_sha1"),
    )


def downgrade() -> None:
    connection = op.get_bind()
    if POINTS not in sa.inspect(connection).get_table_names():
        return
    points = sa.table(POINTS, sa.column("id"))
    if connection.execute(sa.select(points.c.id).limit(1)).first() is not None:
        raise RuntimeError("the recovery points are the audit of which backups covered which photos; roll the schema forward instead of downgrading past 0056")
    op.drop_table(ASSETS)
    op.drop_index("ix_recovery_points_state_opened", table_name=POINTS)
    op.drop_table(POINTS)
