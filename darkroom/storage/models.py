from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


Bytes = Integer().with_variant(BigInteger(), "postgresql")


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


class ManagedImportIntent(Base):
    __tablename__ = "managed_import_intents"
    __table_args__ = (
        UniqueConstraint("person_id", "owner_id", "export_id", name="uq_managed_import_intents_owner_export"),
        CheckConstraint("schema_version = 1", name="ck_managed_import_intents_schema_version"),
        CheckConstraint("length(owner_id) > 0", name="ck_managed_import_intents_owner"),
        Index("ix_managed_import_intents_catalog_binding", "id", "person_id", "owner_id", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    export_id: Mapped[int] = mapped_column(ForeignKey("exports.id", ondelete="RESTRICT"), nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    resource_manifest: Mapped[str] = mapped_column(Text, nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[float] = mapped_column(Float, nullable=False)


class ManagedImportReceipt(Base):
    __tablename__ = "managed_import_receipts"
    __table_args__ = (
        UniqueConstraint("intent_id", "resource_key", "source_sha1", name="uq_managed_import_receipts_resource"),
        one_of("state", ("prepared", "outcome_unknown", "returned", "bytes_verified", "metadata_verified", "committed"), "ck_managed_import_receipts_state"),
        CheckConstraint("expected_bytes >= 0 AND (verified_bytes IS NULL OR verified_bytes >= 0)", name="ck_managed_import_receipts_bytes"),
        CheckConstraint("length(source_sha1) = 40", name="ck_managed_import_receipts_source_sha1"),
        CheckConstraint("length(resource_key) > 0 AND length(compound_key) > 0 AND (asset_id IS NULL OR length(asset_id) > 0)",
                        name="ck_managed_import_receipts_identity"),
        CheckConstraint("upload_outcome IS NULL OR upload_outcome IN ('created', 'duplicate')", name="ck_managed_import_receipts_outcome"),
        CheckConstraint("(state IN ('prepared', 'outcome_unknown') AND asset_id IS NULL AND upload_outcome IS NULL) OR "
                        "(state IN ('returned', 'bytes_verified', 'metadata_verified', 'committed') AND asset_id IS NOT NULL AND upload_outcome IS NOT NULL)",
                        name="ck_managed_import_receipts_asset"),
        CheckConstraint("(state IN ('prepared', 'outcome_unknown', 'returned') AND verified_sha1 IS NULL AND verified_bytes IS NULL AND bytes_verified_at IS NULL) OR "
                        "(state IN ('bytes_verified', 'metadata_verified', 'committed') AND verified_sha1 IS NOT NULL AND verified_sha1 = source_sha1 "
                        "AND verified_bytes IS NOT NULL AND verified_bytes = expected_bytes AND bytes_verified_at IS NOT NULL)",
                        name="ck_managed_import_receipts_verified_bytes"),
        CheckConstraint("(state IN ('prepared', 'outcome_unknown', 'returned', 'bytes_verified') AND metadata_evidence IS NULL AND metadata_verified_at IS NULL) OR "
                        "(state IN ('metadata_verified', 'committed') AND metadata_evidence IS NOT NULL AND metadata_verified_at IS NOT NULL)",
                        name="ck_managed_import_receipts_metadata"),
        CheckConstraint("(state = 'committed' AND committed_at IS NOT NULL) OR (state != 'committed' AND committed_at IS NULL)",
                        name="ck_managed_import_receipts_commit"),
        Index("ix_managed_import_receipts_intent_state", "intent_id", "state"),
        Index("ix_managed_import_receipts_catalog_binding", "id", "intent_id", "asset_id", "source_sha1", "state", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    intent_id: Mapped[int] = mapped_column(ForeignKey("managed_import_intents.id", ondelete="RESTRICT"), nullable=False)
    resource_key: Mapped[str] = mapped_column(String, nullable=False)
    compound_key: Mapped[str] = mapped_column(String, nullable=False)
    source_sha1: Mapped[str] = mapped_column(String, nullable=False)
    expected_bytes: Mapped[int] = mapped_column(Bytes, nullable=False)
    expected_metadata: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="prepared", server_default="prepared")
    asset_id: Mapped[str | None] = mapped_column(String)
    upload_outcome: Mapped[str | None] = mapped_column(String)
    verified_sha1: Mapped[str | None] = mapped_column(String)
    verified_bytes: Mapped[int | None] = mapped_column(Bytes)
    bytes_verified_at: Mapped[float | None] = mapped_column(Float)
    metadata_evidence: Mapped[str | None] = mapped_column(Text)
    metadata_verified_at: Mapped[float | None] = mapped_column(Float)
    committed_at: Mapped[float | None] = mapped_column(Float)
    updated_at: Mapped[float] = mapped_column(Float, nullable=False)


class ManagedAssetVersion(Base):
    __tablename__ = "managed_asset_versions"
    __table_args__ = (
        UniqueConstraint("person_id", "owner_id", "asset_id", "sha1", name="uq_managed_asset_versions_identity"),
        UniqueConstraint("id", "person_id", "owner_id", "asset_id", "sha1", name="uq_managed_asset_versions_binding"),
        CheckConstraint("length(owner_id) > 0 AND length(asset_id) > 0 AND length(sha1) = 40", name="ck_managed_asset_versions_identity"),
        CheckConstraint("bytes IS NULL OR bytes >= 0", name="ck_managed_asset_versions_bytes"),
        CheckConstraint("library_id IS NULL OR length(library_id) > 0", name="ck_managed_asset_versions_library"),
        CheckConstraint("linked_video_asset_id IS NULL OR length(linked_video_asset_id) > 0", name="ck_managed_asset_versions_link"),
        one_of("kind", ("IMAGE", "VIDEO"), "ck_managed_asset_versions_kind"),
        one_of("state", ("observed", "bytes_verified", "missing", "trashed", "offline", "external", "superseded"), "ck_managed_asset_versions_state"),
        CheckConstraint("state != 'bytes_verified' OR (library_id IS NULL AND bytes IS NOT NULL AND bytes_verified_at IS NOT NULL)",
                        name="ck_managed_asset_versions_verified"),
        CheckConstraint("state != 'external' OR library_id IS NOT NULL", name="ck_managed_asset_versions_external"),
        Index("ix_managed_asset_versions_owner_state", "person_id", "owner_id", "state"),
        Index("ix_managed_asset_versions_pixel_binding", "id", "person_id", "owner_id", "asset_id", "sha1", "bytes", unique=True),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="RESTRICT"), nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int | None] = mapped_column(Bytes)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="observed", server_default="observed")
    library_id: Mapped[str | None] = mapped_column(String)
    linked_video_asset_id: Mapped[str | None] = mapped_column(String)
    metadata_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    seen_at: Mapped[float] = mapped_column(Float, nullable=False)
    bytes_verified_at: Mapped[float | None] = mapped_column(Float)


class ManagedAssetOrigin(Base):
    __tablename__ = "managed_asset_origins"
    __table_args__ = (
        UniqueConstraint("version_id", "kind", "source_key", name="uq_managed_asset_origins_source"),
        ForeignKeyConstraint(["version_id", "person_id", "owner_id", "asset_id", "sha1"],
                             ["managed_asset_versions.id", "managed_asset_versions.person_id", "managed_asset_versions.owner_id",
                              "managed_asset_versions.asset_id", "managed_asset_versions.sha1"], ondelete="RESTRICT", name="fk_managed_asset_origins_version"),
        ForeignKeyConstraint(["intent_id", "person_id", "owner_id"],
                             ["managed_import_intents.id", "managed_import_intents.person_id", "managed_import_intents.owner_id"],
                             ondelete="RESTRICT", name="fk_managed_asset_origins_intent_owner"),
        ForeignKeyConstraint(["receipt_id", "intent_id", "asset_id", "sha1", "receipt_state"],
                             ["managed_import_receipts.id", "managed_import_receipts.intent_id", "managed_import_receipts.asset_id",
                              "managed_import_receipts.source_sha1", "managed_import_receipts.state"], ondelete="RESTRICT", name="fk_managed_asset_origins_receipt"),
        one_of("kind", ("phone", "takeout", "legacy", "other"), "ck_managed_asset_origins_kind"),
        CheckConstraint("length(source_key) > 0", name="ck_managed_asset_origins_source"),
        CheckConstraint("(receipt_id IS NULL AND intent_id IS NULL AND receipt_state IS NULL) OR "
                        "(receipt_id IS NOT NULL AND intent_id IS NOT NULL AND receipt_state IS NOT NULL AND receipt_state = 'committed')",
                        name="ck_managed_asset_origins_receipt"),
        CheckConstraint("kind != 'takeout' OR receipt_id IS NOT NULL", name="ck_managed_asset_origins_takeout"),
        CheckConstraint("kind != 'phone' OR receipt_id IS NULL", name="ck_managed_asset_origins_phone"),
        Index("ix_managed_asset_origins_receipt", "receipt_id", "intent_id"),
        Index("ix_managed_asset_origins_intent", "intent_id", "person_id", "owner_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    person_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    source_key: Mapped[str] = mapped_column(String, nullable=False)
    intent_id: Mapped[int | None] = mapped_column(Integer)
    receipt_id: Mapped[int | None] = mapped_column(Integer)
    receipt_state: Mapped[str | None] = mapped_column(String)
    evidence_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)


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
    bytes: Mapped[int | None] = mapped_column(Bytes)
    downloads_used: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending", server_default="pending")
    bytes_done: Mapped[int | None] = mapped_column(Bytes)


class MediaItem(Base):
    __tablename__ = "media_items"
    __table_args__ = (
        UniqueConstraint("person_id", "path", name="uq_media_items_person_path"),
        Index("ix_media_items_person_sha1", "person_id", "sha1"),
        Index("ix_media_items_person_captured", "person_id", "captured_utc"),
        Index("ix_media_items_pixel_source_binding", "id", "person_id", unique=True),
        one_of("source", ("phone", "takeout"), "ck_media_items_source"),
        one_of("time_source", ("exif_offset", "gps_lookup", "sidecar", "unknown"), "ck_media_items_time_source"),
        one_of("gps_source", ("exif", "google", "none"), "ck_media_items_gps_source"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    path: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Bytes, nullable=False)
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
    bytes: Mapped[int] = mapped_column(Bytes, nullable=False)
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
        Index("ix_pixel_copies_source_binding", "id", "pixel_id", "item_id", "person_id", unique=True),
        one_of("state", ("queued", "placed", "pruned"), "ck_pixel_copies_state"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    pixel_id: Mapped[int] = mapped_column(ForeignKey("pixels.id", ondelete="CASCADE"), nullable=False)
    item_id: Mapped[int] = mapped_column(ForeignKey("media_items.id", ondelete="CASCADE"), nullable=False)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False, default="queued", server_default="queued")
    name: Mapped[str | None] = mapped_column(String)
    bytes: Mapped[int] = mapped_column(Bytes, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    queued_at: Mapped[float] = mapped_column(Float, nullable=False)
    placed_at: Mapped[float | None] = mapped_column(Float)
    pruned_at: Mapped[float | None] = mapped_column(Float)
    requeued: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    always_send: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="0")
    seen_again_at: Mapped[float | None] = mapped_column(Float)


class ManagedPixelSource(Base):
    __tablename__ = "managed_pixel_sources"
    __table_args__ = (
        UniqueConstraint("copy_id", "generation", "google_account_id", name="uq_managed_pixel_sources_generation"),
        ForeignKeyConstraint(["copy_id", "pixel_id", "item_id", "person_id"],
                             ["pixel_copies.id", "pixel_copies.pixel_id", "pixel_copies.item_id", "pixel_copies.person_id"],
                             ondelete="RESTRICT", name="fk_managed_pixel_sources_copy"),
        ForeignKeyConstraint(["item_id", "person_id"], ["media_items.id", "media_items.person_id"],
                             ondelete="RESTRICT", name="fk_managed_pixel_sources_item"),
        ForeignKeyConstraint(["version_id", "person_id", "owner_id", "asset_id", "sha1", "bytes"],
                             ["managed_asset_versions.id", "managed_asset_versions.person_id", "managed_asset_versions.owner_id",
                              "managed_asset_versions.asset_id", "managed_asset_versions.sha1", "managed_asset_versions.bytes"],
                             ondelete="RESTRICT", name="fk_managed_pixel_sources_version"),
        CheckConstraint("generation >= 0 AND bytes >= 0", name="ck_managed_pixel_sources_counts"),
        CheckConstraint("length(destination_email) > 0", name="ck_managed_pixel_sources_destination"),
        Index("ix_managed_pixel_sources_version", "version_id"),
        Index("ix_managed_pixel_sources_item", "item_id", "person_id"),
        Index("ix_managed_pixel_sources_account", "google_account_id"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    copy_id: Mapped[int] = mapped_column(Integer, nullable=False)
    pixel_id: Mapped[int] = mapped_column(Integer, nullable=False)
    item_id: Mapped[int] = mapped_column(Integer, nullable=False)
    person_id: Mapped[int] = mapped_column(Integer, nullable=False)
    version_id: Mapped[int] = mapped_column(Integer, nullable=False)
    owner_id: Mapped[str] = mapped_column(String, nullable=False)
    asset_id: Mapped[str] = mapped_column(String, nullable=False)
    sha1: Mapped[str] = mapped_column(String, nullable=False)
    bytes: Mapped[int] = mapped_column(Bytes, nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    google_account_id: Mapped[int] = mapped_column(ForeignKey("google_accounts.id", ondelete="RESTRICT"), nullable=False)
    destination_email: Mapped[str] = mapped_column(String, nullable=False)
    copy_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    recorded_at: Mapped[float] = mapped_column(Float, nullable=False)


class DuplicateRemoval(Base):
    __tablename__ = "duplicate_removals"
    __table_args__ = (
        Index("ix_duplicate_removals_person_state", "person_id", "state"),
        one_of("state", ("pending", "removed", "refused"), "ck_duplicate_removals_state"),
        one_of("rule", ("identical", "phone_better", "takeout_better"), "ck_duplicate_removals_rule"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id", ondelete="CASCADE"), nullable=False)
    decision_id: Mapped[int | None] = mapped_column(ForeignKey("dedupe_decisions.id", ondelete="CASCADE"))
    kept_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    removed_item_id: Mapped[int | None] = mapped_column(ForeignKey("media_items.id", ondelete="SET NULL"))
    kept_path: Mapped[str] = mapped_column(String, nullable=False)
    kept_sha1: Mapped[str] = mapped_column(String, nullable=False)
    removed_path: Mapped[str] = mapped_column(String, nullable=False)
    removed_sha1: Mapped[str] = mapped_column(String, nullable=False)
    removed_source: Mapped[str] = mapped_column(String, nullable=False)
    rule: Mapped[str] = mapped_column(String, nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String, nullable=False)
    reason: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[float] = mapped_column(Float, nullable=False)
    removed_at: Mapped[float | None] = mapped_column(Float)


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
    __table_args__ = (one_of("kind", ("web",), "ck_auth_pending_kind"),)
    state: Mapped[str] = mapped_column(String, primary_key=True)
    nonce: Mapped[str] = mapped_column(String, nullable=False)
    verifier: Mapped[str] = mapped_column(String, nullable=False)
    next: Mapped[str | None] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="web", server_default="web")
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
