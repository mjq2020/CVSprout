"""System RKNN releases and device-pulled deployment leases (migration 15)."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_model_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversion_id INTEGER NOT NULL REFERENCES model_conversions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('building','ready','failed')),
    filename TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    md5 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    contract_json TEXT NOT NULL DEFAULT '{}',
    model_info_json TEXT NOT NULL DEFAULT '{}',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_model_release_conversion
ON edge_model_releases(conversion_id) WHERE status IN ('building','ready');
CREATE TABLE IF NOT EXISTS edge_system_status (
    device_id TEXT PRIMARY KEY REFERENCES edge_devices(id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL,
    credential_hash TEXT NOT NULL,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    current_json TEXT NOT NULL DEFAULT 'null',
    firmware_version TEXT NOT NULL DEFAULT '',
    available_bytes INTEGER,
    error_code TEXT NOT NULL DEFAULT '',
    reported_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edge_model_deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES edge_devices(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    release_id INTEGER NOT NULL REFERENCES edge_model_releases(id) ON DELETE CASCADE,
    activate INTEGER NOT NULL DEFAULT 1,
    fps INTEGER NOT NULL DEFAULT 10,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','unknown','cancelled')),
    stage TEXT NOT NULL DEFAULT 'queued',
    agent_id TEXT,
    credential_hash TEXT,
    lease_hash TEXT,
    lease_expires_at TEXT,
    last_sequence INTEGER NOT NULL DEFAULT -1,
    report_hash TEXT NOT NULL DEFAULT '',
    current_json TEXT NOT NULL DEFAULT 'null',
    rollback_status TEXT NOT NULL DEFAULT 'not_needed',
    error_code TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_model_deployment_active
ON edge_model_deployments(device_id) WHERE status IN ('queued','running','unknown');
CREATE INDEX IF NOT EXISTS idx_edge_model_deployment_project ON edge_model_deployments(project_id,id);
-- Both enqueue directions are protected in SQLite, including concurrent web
-- replicas and legacy SSH operations. Unknown means a mutation may have run.
CREATE TRIGGER IF NOT EXISTS edge_model_blocks_legacy_insert
BEFORE INSERT ON edge_deployments WHEN NEW.status IN ('queued','running')
AND EXISTS (SELECT 1 FROM edge_model_deployments WHERE device_id=NEW.device_id AND status IN ('queued','running','unknown'))
BEGIN SELECT RAISE(ABORT,'system_model_deployment_active'); END;
CREATE TRIGGER IF NOT EXISTS edge_legacy_blocks_model_insert
BEFORE INSERT ON edge_model_deployments WHEN NEW.status IN ('queued','running','unknown')
AND EXISTS (SELECT 1 FROM edge_deployments WHERE device_id=NEW.device_id AND status IN ('queued','running','unknown'))
BEGIN SELECT RAISE(ABORT,'legacy_model_deployment_active'); END;
"""
