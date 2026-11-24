import json

from alembic import op
import sqlalchemy as sa


revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None

HOLDS = ("unknown_offset", "no_instant", "unwritable", "compound_conflict", "unusable")
RESOURCES = ("takeout_entry", "media_item")


def manifest(schema: int, resource: str) -> str:
    return json.dumps({"schema": schema, "resource": resource, "compound": "single" if schema == 1 else "members"}, sort_keys=True)


def rewrite(source: int, target: int) -> None:
    connection = op.get_bind()
    intents = sa.table("managed_import_intents", sa.column("id"), sa.column("resource_manifest"))
    for intent_id, raw in connection.execute(sa.select(intents.c.id, intents.c.resource_manifest).order_by(intents.c.id)).all():
        try:
            found = json.loads(raw)
        except ValueError:
            continue
        resource = next((name for name in RESOURCES if found == json.loads(manifest(source, name))), None)
        if resource is not None:
            connection.execute(sa.update(intents).where(intents.c.id == intent_id).values(resource_manifest=manifest(target, resource)))


def upgrade() -> None:
    byte_count = sa.Integer().with_variant(sa.BigInteger(), "postgresql")
    op.create_table(
        "managed_compounds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.Integer(), nullable=False),
        sa.Column("compound_key", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("hold", sa.String(), nullable=True),
        sa.Column("frozen_at", sa.Float(), nullable=False),
        sa.Column("committed_at", sa.Float(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["intent_id"], ["managed_import_intents.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("intent_id", "compound_key", name="uq_managed_compounds_key"),
        sa.UniqueConstraint("id", "intent_id", name="uq_managed_compounds_intent_binding"),
        sa.CheckConstraint("length(compound_key) > 0", name="ck_managed_compounds_key"),
        sa.CheckConstraint("kind IN ('live_photo')", name="ck_managed_compounds_kind"),
        sa.CheckConstraint(f"hold IS NULL OR hold IN ({', '.join(repr(hold) for hold in HOLDS)})", name="ck_managed_compounds_hold"),
        sa.CheckConstraint("committed_at IS NULL OR hold IS NULL", name="ck_managed_compounds_committed"),
    )
    op.create_table(
        "managed_compound_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("compound_id", sa.Integer(), nullable=False),
        sa.Column("intent_id", sa.Integer(), nullable=False),
        sa.Column("resource_key", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("source_sha1", sa.String(), nullable=False),
        sa.Column("expected_bytes", byte_count, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["compound_id", "intent_id"], ["managed_compounds.id", "managed_compounds.intent_id"], ondelete="RESTRICT",
                                name="fk_managed_compound_members_compound"),
        sa.UniqueConstraint("intent_id", "resource_key", name="uq_managed_compound_members_resource"),
        sa.UniqueConstraint("compound_id", "role", name="uq_managed_compound_members_role"),
        sa.CheckConstraint("role IN ('live_still', 'live_video')", name="ck_managed_compound_members_role"),
        sa.CheckConstraint("length(resource_key) > 0", name="ck_managed_compound_members_resource"),
        sa.CheckConstraint("length(source_sha1) = 40", name="ck_managed_compound_members_source_sha1"),
        sa.CheckConstraint("expected_bytes >= 0", name="ck_managed_compound_members_bytes"),
    )
    rewrite(1, 2)


def downgrade() -> None:
    connection = op.get_bind()
    found = sa.inspect(connection).get_table_names()
    compounds = sa.table("managed_compounds", sa.column("id"))
    if "managed_compounds" in found and connection.execute(sa.select(compounds.c.id).limit(1)).first() is not None:
        raise RuntimeError("Immich Live Photo compounds are kept; roll the schema forward instead of downgrading past 0055")
    rewrite(2, 1)
    if "managed_compound_members" in found:
        op.drop_table("managed_compound_members")
    if "managed_compounds" in found:
        op.drop_table("managed_compounds")
