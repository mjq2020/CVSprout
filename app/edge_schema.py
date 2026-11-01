"""Durable device model releases and non-retrying deployment operations."""

SCHEMA = """
CREATE TABLE IF NOT EXISTS edge_releases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    conversion_id INTEGER NOT NULL REFERENCES model_conversions(id) ON DELETE CASCADE,
    status TEXT NOT NULL CHECK(status IN ('building','ready','failed')),
    version TEXT NOT NULL DEFAULT '',
    sequence INTEGER NOT NULL DEFAULT 1,
    archive_name TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL DEFAULT '',
    size_bytes INTEGER NOT NULL DEFAULT 0,
    contract_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_edge_release_project ON edge_releases(project_id,id);
CREATE TABLE IF NOT EXISTS edge_deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id TEXT NOT NULL REFERENCES edge_devices(id) ON DELETE CASCADE,
    release_id INTEGER NOT NULL REFERENCES edge_releases(id) ON DELETE CASCADE,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    host TEXT NOT NULL,
    port INTEGER NOT NULL,
    username TEXT NOT NULL,
    replace_app INTEGER NOT NULL DEFAULT 0,
    start_app INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','unknown')),
    stage TEXT NOT NULL DEFAULT 'queued',
    error TEXT NOT NULL DEFAULT '',
    report_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    finished_at TEXT,
    lease_expires_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_deployment_active_device
ON edge_deployments(device_id) WHERE status IN ('queued','running');
CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_deployment_active_target
ON edge_deployments(host,port) WHERE status IN ('queued','running');
"""
