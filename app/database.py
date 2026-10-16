from __future__ import annotations

import sqlite3
import re
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Iterator

from .config import settings


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'annotator', 'reviewer')),
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_attempts (
    username TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_start INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS login_ip_attempts (
    ip_address TEXT PRIMARY KEY,
    failures INTEGER NOT NULL DEFAULT 0,
    window_start INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS registration_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    reason TEXT NOT NULL DEFAULT '',
    role TEXT CHECK(role IN ('admin','annotator','reviewer')),
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    ip_address TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reviewed_at TEXT,
    reviewed_by TEXT
);
CREATE INDEX IF NOT EXISTS idx_registration_status_updated ON registration_requests(status,updated_at,id);
CREATE TABLE IF NOT EXISTS registration_ip_attempts (
    ip_address TEXT PRIMARY KEY,
    attempts INTEGER NOT NULL DEFAULT 0,
    window_start INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    shortcut TEXT NOT NULL DEFAULT '',
    attributes_schema_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    storage_name TEXT NOT NULL UNIQUE,
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'unlabeled',
    review_status TEXT NOT NULL DEFAULT 'draft',
    review_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL DEFAULT 0,
    lock_owner TEXT,
    lock_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    label_id INTEGER NOT NULL REFERENCES labels(id) ON DELETE CASCADE,
    shape_type TEXT NOT NULL,
    points_json TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    source TEXT NOT NULL DEFAULT 'manual',
    model_id TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL DEFAULT 'system',
    updated_by TEXT NOT NULL DEFAULT 'system',
    deleted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    model_id TEXT NOT NULL,
    mode TEXT NOT NULL,
    prompt TEXT NOT NULL,
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    created_count INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    claimed_by TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    progress_message TEXT NOT NULL DEFAULT '',
    settings_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS model_services (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    family TEXT NOT NULL,
    provider_type TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    endpoint TEXT NOT NULL DEFAULT '',
    model_name TEXT NOT NULL DEFAULT '',
    api_key_env TEXT NOT NULL DEFAULT '',
    capabilities_json TEXT NOT NULL DEFAULT '[]',
    modes_json TEXT NOT NULL DEFAULT '[]',
    config_json TEXT NOT NULL DEFAULT '{}',
    timeout_seconds INTEGER NOT NULL DEFAULT 180,
    enabled INTEGER NOT NULL DEFAULT 1,
    builtin INTEGER NOT NULL DEFAULT 0,
    last_health_status TEXT NOT NULL DEFAULT 'unknown',
    last_health_error TEXT NOT NULL DEFAULT '',
    last_health_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_asset_results (
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    created_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(job_id, asset_id)
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    project_id INTEGER,
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
    from_status TEXT NOT NULL,
    to_status TEXT NOT NULL,
    actor TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    status TEXT NOT NULL,
    current_job_id INTEGER,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS dataset_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_number INTEGER NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    approved_only INTEGER NOT NULL DEFAULT 1,
    split_seed INTEGER NOT NULL DEFAULT 42,
    train_ratio REAL NOT NULL,
    valid_ratio REAL NOT NULL,
    test_ratio REAL NOT NULL,
    content_hash TEXT NOT NULL,
    asset_count INTEGER NOT NULL,
    annotation_count INTEGER NOT NULL,
    stats_json TEXT NOT NULL DEFAULT '{}',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, version_number)
);

CREATE TABLE IF NOT EXISTS dataset_version_labels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    original_label_id INTEGER NOT NULL,
    category_index INTEGER NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    shortcut TEXT NOT NULL DEFAULT '',
    attributes_schema_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(version_id, original_label_id),
    UNIQUE(version_id, category_index)
);

CREATE TABLE IF NOT EXISTS dataset_version_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    original_asset_id INTEGER NOT NULL,
    split TEXT NOT NULL CHECK(split IN ('train', 'valid', 'test')),
    filename TEXT NOT NULL,
    storage_name TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    width INTEGER NOT NULL,
    height INTEGER NOT NULL,
    size_bytes INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    review_status TEXT NOT NULL,
    review_reason TEXT NOT NULL DEFAULT '',
    revision INTEGER NOT NULL,
    source_created_at TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    variant_index INTEGER NOT NULL DEFAULT 0 CHECK(variant_index >= 0),
    source_version_asset_id INTEGER REFERENCES dataset_version_assets(id),
    augmentation_json TEXT NOT NULL DEFAULT '{}',
    UNIQUE(version_id, original_asset_id, variant_index),
    UNIQUE(version_id, storage_name)
);

CREATE TABLE IF NOT EXISTS dataset_version_annotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE CASCADE,
    version_asset_id INTEGER NOT NULL REFERENCES dataset_version_assets(id) ON DELETE CASCADE,
    version_label_id INTEGER NOT NULL REFERENCES dataset_version_labels(id) ON DELETE RESTRICT,
    original_annotation_id INTEGER NOT NULL,
    shape_type TEXT NOT NULL,
    points_json TEXT NOT NULL,
    attributes_json TEXT NOT NULL DEFAULT '{}',
    confidence REAL,
    source TEXT NOT NULL,
    model_id TEXT,
    source_version INTEGER NOT NULL,
    created_by TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    source_created_at TEXT NOT NULL,
    source_updated_at TEXT NOT NULL,
    UNIQUE(version_asset_id, original_annotation_id)
);

CREATE TABLE IF NOT EXISTS training_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    version_id INTEGER NOT NULL REFERENCES dataset_versions(id) ON DELETE RESTRICT,
    name TEXT NOT NULL,
    model_arch TEXT NOT NULL,
    task TEXT NOT NULL DEFAULT 'detect',
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    current_epoch INTEGER NOT NULL DEFAULT 0,
    total_epochs INTEGER NOT NULL,
    settings_json TEXT NOT NULL DEFAULT '{}',
    metrics_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT,
    error TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    claimed_by TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    parent_run_id INTEGER REFERENCES training_runs(id) ON DELETE SET NULL,
    root_run_id INTEGER REFERENCES training_runs(id) ON DELETE SET NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    config_hash TEXT NOT NULL DEFAULT '',
    resource_json TEXT NOT NULL DEFAULT '{}',
    artifact_metadata_json TEXT NOT NULL DEFAULT '{}',
    progress_message TEXT NOT NULL DEFAULT '',
    resume_count INTEGER NOT NULL DEFAULT 0,
    claim_generation INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS model_conversions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    training_run_id INTEGER NOT NULL REFERENCES training_runs(id) ON DELETE CASCADE,
    format TEXT NOT NULL CHECK(format IN ('torchscript', 'onnx', 'engine', 'rknn')),
    status TEXT NOT NULL DEFAULT 'queued',
    progress REAL NOT NULL DEFAULT 0,
    settings_json TEXT NOT NULL DEFAULT '{}',
    artifact_path TEXT,
    error TEXT,
    claimed_by TEXT,
    lease_expires_at TEXT,
    heartbeat_at TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    parent_conversion_id INTEGER REFERENCES model_conversions(id) ON DELETE SET NULL,
    root_conversion_id INTEGER REFERENCES model_conversions(id) ON DELETE SET NULL,
    attempt INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 2,
    resource_json TEXT NOT NULL DEFAULT '{}',
    artifact_metadata_json TEXT NOT NULL DEFAULT '{}',
    progress_message TEXT NOT NULL DEFAULT '',
    claim_generation INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS training_run_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES training_runs(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES training_runs(id) ON DELETE CASCADE,
    epoch INTEGER NOT NULL,
    progress REAL NOT NULL,
    metrics_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    UNIQUE(run_id, epoch)
);

CREATE TABLE IF NOT EXISTS conversion_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversion_id INTEGER NOT NULL REFERENCES model_conversions(id) ON DELETE CASCADE,
    level TEXT NOT NULL,
    event TEXT NOT NULL,
    message TEXT NOT NULL,
    data_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_resource_leases (
    resource_id TEXT PRIMARY KEY,
    worker_id TEXT NOT NULL,
    task_type TEXT NOT NULL CHECK(task_type IN ('training', 'conversion')),
    task_id INTEGER NOT NULL,
    claim_generation INTEGER NOT NULL,
    leased_at TEXT NOT NULL,
    heartbeat_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_assets_project_status ON assets(project_id, status);
CREATE INDEX IF NOT EXISTS idx_audit_project_created ON audit_logs(project_id, created_at);
CREATE INDEX IF NOT EXISTS idx_workers_seen ON workers(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_versions_project ON dataset_versions(project_id, version_number);
CREATE INDEX IF NOT EXISTS idx_version_assets_split ON dataset_version_assets(version_id, split);
CREATE INDEX IF NOT EXISTS idx_version_assets_original ON dataset_version_assets(original_asset_id);
CREATE INDEX IF NOT EXISTS idx_review_events_asset ON review_events(asset_id);
CREATE INDEX IF NOT EXISTS idx_job_results_asset ON job_asset_results(asset_id);
CREATE INDEX IF NOT EXISTS idx_version_annotations_asset ON dataset_version_annotations(version_asset_id);
CREATE INDEX IF NOT EXISTS idx_training_project_status ON training_runs(project_id, status);
CREATE INDEX IF NOT EXISTS idx_conversions_run_status ON model_conversions(training_run_id, status);
CREATE INDEX IF NOT EXISTS idx_training_logs_run_id ON training_run_logs(run_id, id);
CREATE INDEX IF NOT EXISTS idx_training_metrics_run_epoch ON training_metrics(run_id, epoch);
CREATE INDEX IF NOT EXISTS idx_conversion_logs_conversion_id ON conversion_logs(conversion_id, id);
CREATE INDEX IF NOT EXISTS idx_resource_leases_task ON pipeline_resource_leases(task_type, task_id);
"""


LEGACY_COLUMNS: dict[str, dict[str, str]] = {
    "users": {
        "must_change_password": "INTEGER NOT NULL DEFAULT 0",
        "last_login_at": "TEXT",
        "password_changed_at": "TEXT",
    },
    "user_sessions": {
        "session_id": "TEXT NOT NULL DEFAULT ''",
        "created_at": "TEXT NOT NULL DEFAULT ''",
        "last_seen_at": "TEXT NOT NULL DEFAULT ''",
        "ip_address": "TEXT NOT NULL DEFAULT ''",
        "user_agent": "TEXT NOT NULL DEFAULT ''",
    },
    "projects": {
        "task_type": "TEXT NOT NULL DEFAULT 'mixed'",
        "keypoint_schema_json": "TEXT NOT NULL DEFAULT '{}'",
        "archived_at": "TEXT",
    },
    "dataset_versions": {
        "task_type": "TEXT NOT NULL DEFAULT 'mixed'",
        "keypoint_schema_json": "TEXT NOT NULL DEFAULT '{}'",
        "augmentation_json": "TEXT NOT NULL DEFAULT 'null'",
    },
    "dataset_version_assets": {
        "variant_index": "INTEGER NOT NULL DEFAULT 0 CHECK(variant_index >= 0)",
        "source_version_asset_id": "INTEGER REFERENCES dataset_version_assets(id)",
        "augmentation_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "dataset_version_annotations": {
        "keypoints_json": "TEXT NOT NULL DEFAULT '[]'",
        "human_modified": "INTEGER NOT NULL DEFAULT 0",
    },
    "labels": {
        "attributes_schema_json": "TEXT NOT NULL DEFAULT '{}'",
    },
    "assets": {
        "sha256": "TEXT NOT NULL DEFAULT ''",
        "review_status": "TEXT NOT NULL DEFAULT 'draft'",
        "review_reason": "TEXT NOT NULL DEFAULT ''",
        "revision": "INTEGER NOT NULL DEFAULT 0",
        "lock_owner": "TEXT",
        "lock_expires_at": "TEXT",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    },
    "annotations": {
        "keypoints_json": "TEXT NOT NULL DEFAULT '[]'",
        "human_modified": "INTEGER NOT NULL DEFAULT 0",
        "version": "INTEGER NOT NULL DEFAULT 1",
        "created_by": "TEXT NOT NULL DEFAULT 'system'",
        "updated_by": "TEXT NOT NULL DEFAULT 'system'",
        "deleted_at": "TEXT",
    },
    "jobs": {
        "review_generation": "INTEGER NOT NULL DEFAULT 0",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 2",
        "claimed_by": "TEXT",
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "progress_message": "TEXT NOT NULL DEFAULT ''",
    },
    "training_runs": {
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "parent_run_id": "INTEGER REFERENCES training_runs(id) ON DELETE SET NULL",
        "root_run_id": "INTEGER REFERENCES training_runs(id) ON DELETE SET NULL",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 2",
        "config_hash": "TEXT NOT NULL DEFAULT ''",
        "resource_json": "TEXT NOT NULL DEFAULT '{}'",
        "artifact_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "progress_message": "TEXT NOT NULL DEFAULT ''",
        "resume_count": "INTEGER NOT NULL DEFAULT 0",
        "claim_generation": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
    },
    "model_conversions": {
        "lease_expires_at": "TEXT",
        "heartbeat_at": "TEXT",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "parent_conversion_id": "INTEGER REFERENCES model_conversions(id) ON DELETE SET NULL",
        "root_conversion_id": "INTEGER REFERENCES model_conversions(id) ON DELETE SET NULL",
        "attempt": "INTEGER NOT NULL DEFAULT 0",
        "max_retries": "INTEGER NOT NULL DEFAULT 2",
        "resource_json": "TEXT NOT NULL DEFAULT '{}'",
        "artifact_metadata_json": "TEXT NOT NULL DEFAULT '{}'",
        "progress_message": "TEXT NOT NULL DEFAULT ''",
        "claim_generation": "INTEGER NOT NULL DEFAULT 0",
        "next_attempt_at": "TEXT",
    },
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _migrate_conversion_formats(conn: sqlite3.Connection) -> None:
    """Rebuild only the format CHECK while retaining IDs, child logs and retry links.

    SQLite cannot ALTER a CHECK constraint. The caller disables FK enforcement
    before BEGIN, then performs this rebuild and FK verification atomically.
    """
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='model_conversions'").fetchone()
    if not row or "'rknn'" in row[0]:
        return
    previous_sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='model_conversions'").fetchone()
    indexes = [item[0] for item in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='model_conversions' AND sql IS NOT NULL")]
    sql = re.sub(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?model_conversions\b", "CREATE TABLE model_conversions_rknn", row[0], count=1, flags=re.I)
    sql, changes = re.subn(r"CHECK\s*\(\s*format\s+IN\s*\([^)]*\)\s*\)", "CHECK(format IN ('torchscript', 'onnx', 'engine', 'rknn'))", sql, count=1, flags=re.I)
    if changes != 1 or sql == row[0]:
        raise RuntimeError("Cannot safely migrate the model conversion format constraint")
    columns = ",".join(f'"{item[1]}"' for item in conn.execute("PRAGMA table_info(model_conversions)"))
    conn.execute(sql)
    conn.execute(f"INSERT INTO model_conversions_rknn ({columns}) SELECT {columns} FROM model_conversions")
    conn.execute("DROP TABLE model_conversions")
    conn.execute("ALTER TABLE model_conversions_rknn RENAME TO model_conversions")
    for index in indexes:
        conn.execute(index)
    if previous_sequence:
        conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='model_conversions'", (previous_sequence[0],))
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Foreign-key verification failed while migrating model conversions")


def _migrate_dataset_variants(conn: sqlite3.Connection) -> None:
    """Preserve snapshot IDs and immutable content while allowing true variants."""
    replacements = {
        "dataset_version_assets": (r"UNIQUE\s*\(\s*version_id\s*,\s*original_asset_id\s*\)", "UNIQUE(version_id, original_asset_id, variant_index)"),
        "dataset_version_annotations": (r"UNIQUE\s*\(\s*version_id\s*,\s*original_annotation_id\s*\)", "UNIQUE(version_asset_id, original_annotation_id)"),
    }
    changed = False
    for table, (pattern, replacement) in replacements.items():
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if not row or not re.search(pattern, row[0], re.I):
            continue
        changed = True
        temporary = table + "_variants"
        sql, count = re.subn(pattern, replacement, row[0], count=1, flags=re.I)
        sql, renamed = re.subn(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?" + table + r"\b", "CREATE TABLE " + temporary, sql, count=1, flags=re.I)
        if count != 1 or renamed != 1:
            raise RuntimeError("Cannot safely migrate dataset snapshot variants")
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        dependencies = [item[0] for item in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
        columns = ",".join(f'"{item[1]}"' for item in conn.execute(f"PRAGMA table_info({table})"))
        conn.execute(sql)
        conn.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
        for dependency in dependencies: conn.execute(dependency)
        if sequence:
            updated = conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (sequence[0], table))
            if not updated.rowcount:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, sequence[0]))
    if changed and conn.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Foreign-key verification failed while migrating dataset variants")


def init_db() -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.media_dir.mkdir(parents=True, exist_ok=True)
    settings.thumbnail_dir.mkdir(parents=True, exist_ok=True)
    settings.versions_dir.mkdir(parents=True, exist_ok=True)
    settings.training_dir.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(settings.db_path, timeout=30) as conn:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA foreign_keys=OFF")
        # Serialize column discovery and ALTERs when API replicas start together.
        conn.execute("BEGIN IMMEDIATE")
        for table, columns in LEGACY_COLUMNS.items():
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            for column, definition in columns.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        _migrate_conversion_formats(conn)
        _migrate_dataset_variants(conn)
        conn.execute("UPDATE assets SET updated_at = created_at WHERE updated_at = ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_assets_project_review ON assets(project_id, review_status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_annotations_asset_active ON annotations(asset_id, deleted_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_claim ON jobs(status, lease_expires_at, created_at)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (1, ?)",
            (now_iso(),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (2, ?)",
            (now_iso(),),
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_training_parent ON training_runs(parent_run_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_conversion_parent ON model_conversions(parent_conversion_id)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (3, ?)",
            (now_iso(),),
        )
        # Legacy rows have NULL lineage identifiers and are intentionally excluded.
        # New retry rows always set root/parent ids, so these partial indexes make
        # the check-and-insert invariant durable even across multiple web replicas.
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_training_retry_parent_unique
               ON training_runs(parent_run_id) WHERE parent_run_id IS NOT NULL"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_training_lineage_attempt_unique
               ON training_runs(root_run_id, attempt) WHERE root_run_id IS NOT NULL"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_retry_parent_unique
               ON model_conversions(parent_conversion_id)
               WHERE parent_conversion_id IS NOT NULL"""
        )
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_conversion_lineage_attempt_unique
               ON model_conversions(root_conversion_id, attempt)
               WHERE root_conversion_id IS NOT NULL"""
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (4, ?)",
            (now_iso(),),
        )
        conn.execute("""CREATE UNIQUE INDEX IF NOT EXISTS idx_single_classification
            ON annotations(asset_id) WHERE shape_type = 'classification' AND deleted_at IS NULL""")
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (5, ?)",
            (now_iso(),),
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations (version, applied_at) VALUES (6, ?)",
            (now_iso(),),
        )
        conn.execute("UPDATE user_sessions SET session_id=lower(hex(randomblob(16))) WHERE session_id=''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_user_sessions_public_id ON user_sessions(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_sessions_user_expiry ON user_sessions(user_id,expires_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_role_enabled ON users(role,enabled)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_user_events ON audit_logs(resource_type,resource_id,id)")
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (7,?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (8,?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (9,?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (10,?)", (now_iso(),))
        conn.execute("INSERT OR IGNORE INTO schema_migrations (version,applied_at) VALUES (11,?)", (now_iso(),))


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def one(query: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute(query, params).fetchone()
        return dict(row) if row else None


def all_rows(query: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    with db() as conn:
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def audit(
    actor: str,
    action: str,
    resource_type: str,
    resource_id: str | int,
    project_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    import json

    with db() as conn:
        conn.execute(
            """INSERT INTO audit_logs
               (actor, action, resource_type, resource_id, project_id, payload_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                actor,
                action,
                resource_type,
                str(resource_id),
                project_id,
                json.dumps(payload or {}, ensure_ascii=False),
                now_iso(),
            ),
        )
