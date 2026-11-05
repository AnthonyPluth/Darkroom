from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE exports ADD COLUMN owner_person_id INTEGER REFERENCES people(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE exports DROP COLUMN owner_person_id")
