from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def one_of(column: str, values: tuple[str, ...], name: str) -> CheckConstraint:
    return CheckConstraint(f"{column} IN ({', '.join(repr(v) for v in values)})", name=name)


class Person(Base):
    __tablename__ = "people"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    slug: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    email: Mapped[str | None] = mapped_column(String)
    immich_user_id: Mapped[str | None] = mapped_column(String)
    immich_library_id: Mapped[str | None] = mapped_column(String)
    immich_api_key_sealed: Mapped[str | None] = mapped_column(Text)
    originals_path: Mapped[str] = mapped_column(String, nullable=False)
    google_only_path: Mapped[str] = mapped_column(String, nullable=False)
    legacy_path: Mapped[str | None] = mapped_column(String)
    immich_reconciled_until: Mapped[float | None] = mapped_column(Float)
    created: Mapped[float] = mapped_column(Float, nullable=False)


class Device(Base):
    __tablename__ = "devices"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    name: Mapped[str] = mapped_column(String, nullable=False)
    token_hash: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    sub: Mapped[str | None] = mapped_column(String)
    cleanup_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    last_checkin: Mapped[float | None] = mapped_column(Float)
    app_version: Mapped[str | None] = mapped_column(String)
    pending_uploads: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    created: Mapped[float] = mapped_column(Float, nullable=False)
    revoked_at: Mapped[float | None] = mapped_column(Float)
    eligibility_checked_at: Mapped[float | None] = mapped_column(Float)
    push_token_sealed: Mapped[str | None] = mapped_column(Text)
    push_token_digest: Mapped[str | None] = mapped_column(String)
    push_token_at: Mapped[float | None] = mapped_column(Float)
    wake_pushed_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (Index("ix_devices_push_token_digest", "push_token_digest", unique=True),)


class GoogleAccount(Base):
    __tablename__ = "google_accounts"
    __table_args__ = (one_of("session_state", ("unknown", "ok", "needs_sign_in"), "ck_google_accounts_session_state"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String, nullable=False, unique=True)
    default_person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    profile_path: Mapped[str | None] = mapped_column(String)
    session_state: Mapped[str] = mapped_column(String, nullable=False, default="unknown", server_default="unknown")
    session_checked: Mapped[float | None] = mapped_column(Float)
    schedule_created: Mapped[float | None] = mapped_column(Float)
    schedule_end: Mapped[float | None] = mapped_column(Float)
    created: Mapped[float] = mapped_column(Float, nullable=False)


class Pixel(Base):
    __tablename__ = "pixels"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    syncthing_device_id: Mapped[str | None] = mapped_column(String)
    google_account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"), nullable=False)
    last_sync: Mapped[float | None] = mapped_column(Float)
    created: Mapped[float] = mapped_column(Float, nullable=False)


class PixelPerson(Base):
    __tablename__ = "pixel_people"
    pixel_id: Mapped[int] = mapped_column(ForeignKey("pixels.id", ondelete="CASCADE"), primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), primary_key=True)


class PhoneAsset(Base):
    __tablename__ = "phone_assets"
    __table_args__ = (UniqueConstraint("device_id", "photokit_id", name="uq_phone_assets_device_photokit"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    device_id: Mapped[int] = mapped_column(ForeignKey("devices.id", ondelete="CASCADE"), nullable=False)
    photokit_id: Mapped[str] = mapped_column(String, nullable=False)
    created: Mapped[float] = mapped_column(Float, nullable=False)
    eligible_at: Mapped[float | None] = mapped_column(Float)
    eligible_confirmed_at: Mapped[float | None] = mapped_column(Float)
    deleted_at: Mapped[float | None] = mapped_column(Float)


class Export(Base):
    __tablename__ = "exports"
    __table_args__ = (
        one_of("kind", ("initial", "incremental", "manual"), "ck_exports_kind"),
        one_of("status", ("announced", "downloading", "downloaded", "unpacking", "importing", "imported", "failed", "expired"), "ck_exports_status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    google_id: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[float | None] = mapped_column(Float)
    ready_at: Mapped[float | None] = mapped_column(Float)
    expires_at: Mapped[float | None] = mapped_column(Float)
    part_count: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, nullable=False, default="announced", server_default="announced")
    stage: Mapped[str | None] = mapped_column(String)
    inbox_dir: Mapped[str | None] = mapped_column(String)
    download_tries: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    owner_person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))


class ExportPart(Base):
    __tablename__ = "export_parts"
    __table_args__ = (
        UniqueConstraint("export_id", "number", name="uq_export_parts_number"),
        one_of("status", ("pending", "downloading", "done", "failed", "corrupt"), "ck_export_parts_status"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[int] = mapped_column(ForeignKey("exports.id", ondelete="CASCADE"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str | None] = mapped_column(String)
    bytes: Mapped[int | None] = mapped_column(Integer)
    downloads_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", server_default="pending")
    bytes_done: Mapped[int | None] = mapped_column(Integer)


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("person_id", "path", name="uq_media_items_person_path"),
        Index("ix_media_items_person_sha1", "person_id", "sha1"),
        Index("ix_media_items_person_captured", "person_id", "captured_utc"),
        one_of("source", ("phone", "takeout"), "ck_media_items_source"),
        one_of("time_source", ("exif_offset", "gps_lookup", "sidecar", "unknown"), "ck_media_items_time_source"),
        one_of("gps_source", ("exif", "google", "none"), "ck_media_items_gps_source"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime: Mapped[float | None] = mapped_column(Float)
    phash: Mapped[str | None] = mapped_column(String)
    dhash: Mapped[str | None] = mapped_column(String)
    captured_utc: Mapped[float | None] = mapped_column(Float)
    captured_local: Mapped[str | None] = mapped_column(String)
    offset_minutes: Mapped[int | None] = mapped_column(Integer)
    time_source: Mapped[str] = mapped_column(String, nullable=False, default="unknown", server_default="unknown")
    width: Mapped[int | None] = mapped_column(Integer)
    height: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    make: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    gps_source: Mapped[str] = mapped_column(String, nullable=False, default="none", server_default="none")
    confirmed_in_google: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    sidecar_json: Mapped[str | None] = mapped_column(Text)
    export_id: Mapped[int | None] = mapped_column(ForeignKey("exports.id", ondelete="SET NULL"))
    indexed_at: Mapped[float] = mapped_column(Float, nullable=False)


class LegacyFile(Base):
    __tablename__ = "legacy_files"
    __table_args__ = (
        UniqueConstraint("person_id", "path", name="uq_legacy_files_person_path"),
        one_of("status", ("matched", "unmatched", "failed"), "ck_legacy_files_status"),
        CheckConstraint("tier IS NULL OR tier BETWEEN 1 AND 5", name="ck_legacy_files_tier"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mtime: Mapped[float] = mapped_column(Float, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[int | None] = mapped_column(Integer)
    matched_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    original_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    queued_at: Mapped[float | None] = mapped_column(Float)


class ImmichAsset(Base):
    __tablename__ = "immich_assets"
    __table_args__ = (
        UniqueConstraint("person_id", "asset_id", name="uq_immich_assets_person_asset"),
        one_of("status", ("stored", "waiting", "ignored", "failed"), "ck_immich_assets_status"),
        one_of("kind", ("photo", "live_video"), "ck_immich_assets_kind"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    linked_asset_id: Mapped[str | None] = mapped_column(String)
    original_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    queued_at: Mapped[float | None] = mapped_column(Float)


class PhoneResource(Base):
    __tablename__ = "phone_resources"
    __table_args__ = (
        UniqueConstraint("asset_id", "kind", name="uq_phone_resources_asset_kind"),
        one_of("kind", ("photo", "live_video", "adjusted"), "ck_phone_resources_kind"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("phone_assets.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String, nullable=False)
    uploaded_at: Mapped[float | None] = mapped_column(Float)
    media_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))


class DedupeDecision(Base):
    __tablename__ = "dedupe_decisions"
    __table_args__ = (
        CheckConstraint("tier BETWEEN 1 AND 5", name="ck_dedupe_decisions_tier"),
        one_of("decided_by", ("auto", "owner"), "ck_dedupe_decisions_decided_by"),
        UniqueConstraint("export_id", "source_path", name="uq_dedupe_decisions_export_source"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[int] = mapped_column(ForeignKey("exports.id", ondelete="CASCADE"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"), nullable=False)
    source_path: Mapped[str | None] = mapped_column(String)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    takeout_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    matched_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    decided_by: Mapped[str] = mapped_column(String, nullable=False)
    decided_at: Mapped[float] = mapped_column(Float, nullable=False)
    reversed_at: Mapped[float | None] = mapped_column(Float)


class PixelCopy(Base):
    __tablename__ = "pixel_copies"
    __table_args__ = (
        UniqueConstraint("pixel_id", "item_id", name="uq_pixel_copies_pixel_item"),
        Index("ix_pixel_copies_pixel_state", "pixel_id", "state"),
        one_of("state", ("queued", "placed", "pruned"), "ck_pixel_copies_state"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pixel_id: Mapped[int] = mapped_column(ForeignKey("pixels.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="queued", server_default="queued")
    name: Mapped[str | None] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    queued_at: Mapped[float] = mapped_column(Float, nullable=False)
    placed_at: Mapped[float | None] = mapped_column(Float)
    pruned_at: Mapped[float | None] = mapped_column(Float)
    requeued: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    always_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    seen_again_at: Mapped[float | None] = mapped_column(Float)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (one_of("status", ("pending", "kept_both", "skipped", "undone"), "ck_review_items_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("dedupe_decisions.id", ondelete="CASCADE"), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", server_default="pending")
    hold_path: Mapped[str | None] = mapped_column(String)
    resolved_at: Mapped[float | None] = mapped_column(Float)
    sidecar_json: Mapped[str | None] = mapped_column(Text)
    hold_sha1: Mapped[str | None] = mapped_column(String)


class ImportRun(Base):
    __tablename__ = "import_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    export_id: Mapped[int] = mapped_column(ForeignKey("exports.id", ondelete="CASCADE"), nullable=False)
    started: Mapped[float] = mapped_column(Float, nullable=False)
    finished: Mapped[float | None] = mapped_column(Float)
    stage: Mapped[str | None] = mapped_column(String)
    added: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_t1: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_t2: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    skipped_t3: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    review: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    missing_gps: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    unmatched_by_person: Mapped[str | None] = mapped_column(Text)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    repeated: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    changed_people: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String, nullable=False, default="running", server_default="running")
    error: Mapped[str | None] = mapped_column(Text)


class BrowserJob(Base):
    __tablename__ = "browser_jobs"
    __table_args__ = (one_of("status", ("queued", "running", "waiting_for_user", "done", "failed"), "ck_browser_jobs_status"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="queued", server_default="queued")
    payload: Mapped[str | None] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(Text)
    created: Mapped[float] = mapped_column(Float, nullable=False)
    started: Mapped[float | None] = mapped_column(Float)
    heartbeat: Mapped[float | None] = mapped_column(Float)
    finished: Mapped[float | None] = mapped_column(Float)
    failure_dir: Mapped[str | None] = mapped_column(String)
    message: Mapped[str | None] = mapped_column(Text)


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    link: Mapped[str | None] = mapped_column(String)
    opened: Mapped[float] = mapped_column(Float, nullable=False)
    last_sent: Mapped[float | None] = mapped_column(Float)
    sent_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    closed: Mapped[float | None] = mapped_column(Float)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("google_accounts.id", ondelete="SET NULL"))
    push_due: Mapped[float | None] = mapped_column(Float)
    pushed_devices: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_alerts_key", "key"),)


class Event(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    time: Mapped[float] = mapped_column(Float, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False)
    subject: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (Index("ix_events_time", "time"),)


class AuthPending(Base):
    __tablename__ = "auth_pending"
    __table_args__ = (one_of("kind", ("web", "device"), "ck_auth_pending_kind"),)
    state: Mapped[str] = mapped_column(String, primary_key=True)
    nonce: Mapped[str] = mapped_column(String, nullable=False)
    verifier: Mapped[str] = mapped_column(String, nullable=False)
    next: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="web", server_default="web")
    device_name: Mapped[str | None] = mapped_column(String)
    client_state: Mapped[str | None] = mapped_column(String)
    created: Mapped[float] = mapped_column(Float, nullable=False)


class User(Base):
    __tablename__ = "users"
    sub: Mapped[str] = mapped_column(String, primary_key=True)
    email: Mapped[str | None] = mapped_column(String)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    name: Mapped[str | None] = mapped_column(String)
    groups: Mapped[str | None] = mapped_column(Text)
    last_seen: Mapped[float | None] = mapped_column(Float)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String, primary_key=True)
    sub: Mapped[str] = mapped_column(ForeignKey("users.sub", ondelete="CASCADE"), nullable=False)
    created: Mapped[float] = mapped_column(Float, nullable=False)
    expires: Mapped[float | None] = mapped_column(Float)
    refresh_token_sealed: Mapped[str | None] = mapped_column(Text)
    last_checked: Mapped[float | None] = mapped_column(Float)
    cookie_renewed: Mapped[float | None] = mapped_column(Float)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str | None] = mapped_column(Text)
