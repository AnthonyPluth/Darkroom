from alembic import op
import sqlalchemy as sa


revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table('alerts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('subject', sa.String(), nullable=False),
    sa.Column('message', sa.Text(), nullable=False),
    sa.Column('link', sa.String(), nullable=True),
    sa.Column('opened', sa.Float(), nullable=False),
    sa.Column('last_sent', sa.Float(), nullable=True),
    sa.Column('sent_count', sa.Integer(), server_default='0', nullable=False),
    sa.Column('closed', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.create_index('ix_alerts_key', ['key'], unique=False)

    op.create_table('auth_pending',
    sa.Column('state', sa.String(), nullable=False),
    sa.Column('nonce', sa.String(), nullable=False),
    sa.Column('verifier', sa.String(), nullable=False),
    sa.Column('next', sa.String(), nullable=True),
    sa.Column('kind', sa.String(), server_default='web', nullable=False),
    sa.Column('device_name', sa.String(), nullable=True),
    sa.Column('created', sa.Float(), nullable=False),
    sa.CheckConstraint("kind IN ('web', 'device')", name='ck_auth_pending_kind'),
    sa.PrimaryKeyConstraint('state')
    )
    op.create_table('events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('time', sa.Float(), nullable=False),
    sa.Column('type', sa.String(), nullable=False),
    sa.Column('subject', sa.String(), nullable=False),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.create_index('ix_events_time', ['time'], unique=False)

    op.create_table('people',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('slug', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=True),
    sa.Column('immich_user_id', sa.String(), nullable=True),
    sa.Column('immich_library_id', sa.String(), nullable=True),
    sa.Column('immich_api_key_sealed', sa.Text(), nullable=True),
    sa.Column('originals_path', sa.String(), nullable=False),
    sa.Column('google_only_path', sa.String(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('slug')
    )
    op.create_table('settings',
    sa.Column('key', sa.String(), nullable=False),
    sa.Column('value', sa.Text(), nullable=True),
    sa.PrimaryKeyConstraint('key')
    )
    op.create_table('users',
    sa.Column('sub', sa.String(), nullable=False),
    sa.Column('email', sa.String(), nullable=True),
    sa.Column('name', sa.String(), nullable=True),
    sa.Column('groups', sa.Text(), nullable=True),
    sa.Column('last_seen', sa.Float(), nullable=True),
    sa.PrimaryKeyConstraint('sub')
    )
    op.create_table('auth_sessions',
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('sub', sa.String(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('expires', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['sub'], ['users.sub'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('token_hash')
    )
    op.create_table('devices',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('token_hash', sa.String(), nullable=False),
    sa.Column('cleanup_enabled', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('last_checkin', sa.Float(), nullable=True),
    sa.Column('app_version', sa.String(), nullable=True),
    sa.Column('pending_uploads', sa.Integer(), server_default='0', nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('revoked_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('token_hash')
    )
    op.create_table('google_accounts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(), nullable=False),
    sa.Column('default_person_id', sa.Integer(), nullable=False),
    sa.Column('profile_path', sa.String(), nullable=True),
    sa.Column('session_state', sa.String(), server_default='unknown', nullable=False),
    sa.Column('session_checked', sa.Float(), nullable=True),
    sa.Column('schedule_created', sa.Float(), nullable=True),
    sa.Column('schedule_end', sa.Float(), nullable=True),
    sa.Column('created', sa.Float(), nullable=False),
    sa.CheckConstraint("session_state IN ('unknown', 'ok', 'needs_sign_in')", name='ck_google_accounts_session_state'),
    sa.ForeignKeyConstraint(['default_person_id'], ['people.id'], ondelete='RESTRICT'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('email')
    )
    op.create_table('browser_jobs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('status', sa.String(), server_default='queued', nullable=False),
    sa.Column('payload', sa.Text(), nullable=True),
    sa.Column('result', sa.Text(), nullable=True),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('started', sa.Float(), nullable=True),
    sa.Column('heartbeat', sa.Float(), nullable=True),
    sa.Column('finished', sa.Float(), nullable=True),
    sa.Column('failure_dir', sa.String(), nullable=True),
    sa.Column('message', sa.Text(), nullable=True),
    sa.CheckConstraint("status IN ('queued', 'running', 'waiting_for_user', 'done', 'failed')", name='ck_browser_jobs_status'),
    sa.ForeignKeyConstraint(['account_id'], ['google_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('exports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('google_id', sa.String(), nullable=True),
    sa.Column('created_at', sa.Float(), nullable=True),
    sa.Column('ready_at', sa.Float(), nullable=True),
    sa.Column('expires_at', sa.Float(), nullable=True),
    sa.Column('part_count', sa.Integer(), nullable=True),
    sa.Column('status', sa.String(), server_default='announced', nullable=False),
    sa.Column('stage', sa.String(), nullable=True),
    sa.Column('inbox_dir', sa.String(), nullable=True),
    sa.CheckConstraint("kind IN ('initial', 'incremental', 'manual')", name='ck_exports_kind'),
    sa.CheckConstraint("status IN ('announced', 'downloading', 'downloaded', 'unpacking', 'importing', 'imported', 'failed', 'expired')", name='ck_exports_status'),
    sa.ForeignKeyConstraint(['account_id'], ['google_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('phone_assets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('device_id', sa.Integer(), nullable=False),
    sa.Column('photokit_id', sa.String(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.Column('eligible_at', sa.Float(), nullable=True),
    sa.Column('eligible_confirmed_at', sa.Float(), nullable=True),
    sa.Column('deleted_at', sa.Float(), nullable=True),
    sa.ForeignKeyConstraint(['device_id'], ['devices.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('device_id', 'photokit_id', name='uq_phone_assets_device_photokit')
    )
    op.create_table('pixels',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(), nullable=False),
    sa.Column('syncthing_device_id', sa.String(), nullable=True),
    sa.Column('google_account_id', sa.Integer(), nullable=False),
    sa.Column('created', sa.Float(), nullable=False),
    sa.ForeignKeyConstraint(['google_account_id'], ['google_accounts.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('export_parts',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('export_id', sa.Integer(), nullable=False),
    sa.Column('number', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(), nullable=True),
    sa.Column('bytes', sa.Integer(), nullable=True),
    sa.Column('downloads_used', sa.Integer(), server_default='0', nullable=False),
    sa.Column('attempts', sa.Integer(), server_default='0', nullable=False),
    sa.Column('status', sa.String(), server_default='pending', nullable=False),
    sa.CheckConstraint("status IN ('pending', 'downloading', 'done', 'failed', 'corrupt')", name='ck_export_parts_status'),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('export_id', 'number', name='uq_export_parts_number')
    )
    op.create_table('import_runs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('export_id', sa.Integer(), nullable=False),
    sa.Column('started', sa.Float(), nullable=False),
    sa.Column('finished', sa.Float(), nullable=True),
    sa.Column('stage', sa.String(), nullable=True),
    sa.Column('added', sa.Integer(), server_default='0', nullable=False),
    sa.Column('skipped_t1', sa.Integer(), server_default='0', nullable=False),
    sa.Column('skipped_t2', sa.Integer(), server_default='0', nullable=False),
    sa.Column('skipped_t3', sa.Integer(), server_default='0', nullable=False),
    sa.Column('review', sa.Integer(), server_default='0', nullable=False),
    sa.Column('missing_gps', sa.Integer(), server_default='0', nullable=False),
    sa.Column('unmatched_by_person', sa.Text(), nullable=True),
    sa.Column('status', sa.String(), server_default='running', nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('media_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.Column('source', sa.String(), nullable=False),
    sa.Column('path', sa.String(), nullable=False),
    sa.Column('sha1', sa.String(), nullable=False),
    sa.Column('bytes', sa.Integer(), nullable=False),
    sa.Column('mtime', sa.Float(), nullable=True),
    sa.Column('phash', sa.String(), nullable=True),
    sa.Column('captured_utc', sa.Float(), nullable=True),
    sa.Column('captured_local', sa.String(), nullable=True),
    sa.Column('offset_minutes', sa.Integer(), nullable=True),
    sa.Column('time_source', sa.String(), server_default='unknown', nullable=False),
    sa.Column('width', sa.Integer(), nullable=True),
    sa.Column('height', sa.Integer(), nullable=True),
    sa.Column('duration_ms', sa.Integer(), nullable=True),
    sa.Column('make', sa.String(), nullable=True),
    sa.Column('model', sa.String(), nullable=True),
    sa.Column('lat', sa.Float(), nullable=True),
    sa.Column('lon', sa.Float(), nullable=True),
    sa.Column('gps_source', sa.String(), server_default='none', nullable=False),
    sa.Column('confirmed_in_google', sa.Boolean(), server_default='0', nullable=False),
    sa.Column('sidecar_json', sa.Text(), nullable=True),
    sa.Column('export_id', sa.Integer(), nullable=True),
    sa.Column('indexed_at', sa.Float(), nullable=False),
    sa.CheckConstraint("gps_source IN ('exif', 'google', 'none')", name='ck_media_items_gps_source'),
    sa.CheckConstraint("source IN ('phone', 'takeout')", name='ck_media_items_source'),
    sa.CheckConstraint("time_source IN ('exif_offset', 'gps_lookup', 'sidecar', 'unknown')", name='ck_media_items_time_source'),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('person_id', 'path', name='uq_media_items_person_path')
    )
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.create_index('ix_media_items_person_captured', ['person_id', 'captured_utc'], unique=False)
        batch_op.create_index('ix_media_items_person_sha1', ['person_id', 'sha1'], unique=False)

    op.create_table('pixel_people',
    sa.Column('pixel_id', sa.Integer(), nullable=False),
    sa.Column('person_id', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['person_id'], ['people.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['pixel_id'], ['pixels.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('pixel_id', 'person_id')
    )
    op.create_table('dedupe_decisions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('export_id', sa.Integer(), nullable=False),
    sa.Column('account_id', sa.Integer(), nullable=False),
    sa.Column('takeout_item_id', sa.Integer(), nullable=True),
    sa.Column('matched_item_id', sa.Integer(), nullable=True),
    sa.Column('tier', sa.Integer(), nullable=False),
    sa.Column('reason', sa.Text(), nullable=False),
    sa.Column('decided_by', sa.String(), nullable=False),
    sa.Column('decided_at', sa.Float(), nullable=False),
    sa.Column('reversed_at', sa.Float(), nullable=True),
    sa.CheckConstraint("decided_by IN ('auto', 'owner')", name='ck_dedupe_decisions_decided_by'),
    sa.CheckConstraint('tier BETWEEN 1 AND 5', name='ck_dedupe_decisions_tier'),
    sa.ForeignKeyConstraint(['account_id'], ['google_accounts.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['export_id'], ['exports.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['matched_item_id'], ['media_items.id'], ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['takeout_item_id'], ['media_items.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_table('phone_resources',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('asset_id', sa.Integer(), nullable=False),
    sa.Column('kind', sa.String(), nullable=False),
    sa.Column('sha1', sa.String(), nullable=False),
    sa.Column('bytes', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(), nullable=False),
    sa.Column('uploaded_at', sa.Float(), nullable=True),
    sa.Column('media_item_id', sa.Integer(), nullable=True),
    sa.CheckConstraint("kind IN ('photo', 'live_video', 'adjusted')", name='ck_phone_resources_kind'),
    sa.ForeignKeyConstraint(['asset_id'], ['phone_assets.id'], ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['media_item_id'], ['media_items.id'], ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('asset_id', 'kind', name='uq_phone_resources_asset_kind')
    )
    op.create_table('review_items',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('decision_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(), server_default='pending', nullable=False),
    sa.Column('hold_path', sa.String(), nullable=True),
    sa.Column('resolved_at', sa.Float(), nullable=True),
    sa.CheckConstraint("status IN ('pending', 'kept_both', 'skipped', 'undone')", name='ck_review_items_status'),
    sa.ForeignKeyConstraint(['decision_id'], ['dedupe_decisions.id'], ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('decision_id')
    )


def downgrade() -> None:
    op.drop_table('review_items')
    op.drop_table('phone_resources')
    op.drop_table('dedupe_decisions')
    op.drop_table('pixel_people')
    with op.batch_alter_table('media_items', schema=None) as batch_op:
        batch_op.drop_index('ix_media_items_person_sha1')
        batch_op.drop_index('ix_media_items_person_captured')

    op.drop_table('media_items')
    op.drop_table('import_runs')
    op.drop_table('export_parts')
    op.drop_table('pixels')
    op.drop_table('phone_assets')
    op.drop_table('exports')
    op.drop_table('browser_jobs')
    op.drop_table('google_accounts')
    op.drop_table('devices')
    op.drop_table('auth_sessions')
    op.drop_table('users')
    op.drop_table('settings')
    op.drop_table('people')
    with op.batch_alter_table('events', schema=None) as batch_op:
        batch_op.drop_index('ix_events_time')

    op.drop_table('events')
    op.drop_table('auth_pending')
    with op.batch_alter_table('alerts', schema=None) as batch_op:
        batch_op.drop_index('ix_alerts_key')

    op.drop_table('alerts')
