from alembic import op
import sqlalchemy as sa


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("alerts", schema=None, recreate="always") as batch_op:
        batch_op.add_column(sa.Column("person_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("account_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("push_due", sa.Float(), nullable=True))
        batch_op.create_foreign_key("fk_alerts_person_id", "people", ["person_id"], ["id"], ondelete="SET NULL")
        batch_op.create_foreign_key("fk_alerts_account_id", "google_accounts", ["account_id"], ["id"], ondelete="SET NULL")
    op.execute("UPDATE alerts SET person_id = (SELECT id FROM people WHERE people.slug = alerts.subject) "
               "WHERE kind = 'originals_unmatched' OR key LIKE 'immich:failed:%'")
    op.execute("UPDATE alerts SET account_id = (SELECT id FROM google_accounts WHERE google_accounts.email = alerts.subject) "
               "WHERE kind IN ('sign_in_needed', 'wrong_account')")
    op.execute("UPDATE alerts SET account_id = (SELECT account_id FROM exports WHERE 'export ' || exports.id = alerts.subject) "
               "WHERE subject LIKE 'export %'")
    op.execute("UPDATE alerts SET account_id = (SELECT account_id FROM browser_jobs WHERE 'browser:failed:' || browser_jobs.id = alerts.key) "
               "WHERE key LIKE 'browser:failed:%'")


def downgrade() -> None:
    with op.batch_alter_table("alerts", schema=None, recreate="always") as batch_op:
        batch_op.drop_constraint("fk_alerts_account_id", type_="foreignkey")
        batch_op.drop_constraint("fk_alerts_person_id", type_="foreignkey")
        batch_op.drop_column("push_due")
        batch_op.drop_column("account_id")
        batch_op.drop_column("person_id")
