from alembic import op
import sqlalchemy as sa


revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None

TABLE = "immich_assets"
CHECK = "ck_immich_assets_status"
WITH_CATALOGED = "status IN ('stored', 'waiting', 'ignored', 'failed', 'cataloged')"
BEFORE = "status IN ('stored', 'waiting', 'ignored', 'failed')"


def replace_check(rule: str) -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_constraint(CHECK, TABLE, type_="check")
        op.create_check_constraint(CHECK, TABLE, rule)
        return
    op.execute(f"DROP TABLE IF EXISTS _alembic_tmp_{TABLE}")
    with op.batch_alter_table(TABLE, schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint(CHECK, type_="check")
        batch_op.create_check_constraint(CHECK, rule)


def upgrade() -> None:
    replace_check(WITH_CATALOGED)


def downgrade() -> None:
    assets = sa.table(TABLE, sa.column("id"), sa.column("status"))
    if op.get_bind().execute(sa.select(assets.c.id).where(assets.c.status == "cataloged").limit(1)).first() is not None:
        raise RuntimeError("phone assets are catalogued without a copy in originals; roll the schema forward instead of downgrading past 0065")
    replace_check(BEFORE)
