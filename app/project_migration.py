"""One-time conversion from a shared installation to personal projects."""
import json
import os
import re


def migrate(conn, now):
    if conn.execute("SELECT 1 FROM schema_migrations WHERE version=16").fetchone():
        return
    for table in ("users", "registration_requests"):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
        if "'user'" in sql or not re.search(r"CHECK\s*\(\s*role\s+IN", sql, re.I):
            continue
        temporary = table + "_workspace"
        sql = re.sub(r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?" + table + r"\b", "CREATE TABLE " + temporary, sql, count=1, flags=re.I)
        sql, changed = re.subn(r"CHECK\s*\(role\s+IN\s*\([^)]*\)\)",
            "CHECK(role IN ('admin','user','annotator','reviewer'))", sql, count=1, flags=re.I)
        if changed != 1:
            raise RuntimeError("Cannot safely migrate account roles")
        columns = ",".join('"' + row[1] + '"' for row in conn.execute(f"PRAGMA table_info({table})"))
        dependencies = [row[0] for row in conn.execute("SELECT sql FROM sqlite_master WHERE tbl_name=? AND type IN ('index','trigger') AND sql IS NOT NULL", (table,))]
        sequence = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        conn.execute(sql)
        conn.execute(f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}")
        conn.execute(f"DROP TABLE {table}")
        conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
        for statement in dependencies:
            conn.execute(statement)
        if sequence:
            updated = conn.execute("UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name=?", (sequence[0], table))
            if not updated.rowcount:
                conn.execute("INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)", (table, sequence[0]))
    if "owner_user_id" not in {row[1] for row in conn.execute("PRAGMA table_info(projects)")}:
        conn.execute("ALTER TABLE projects ADD COLUMN owner_user_id INTEGER REFERENCES users(id) ON DELETE RESTRICT")
    conn.execute("""CREATE TABLE IF NOT EXISTS project_members (
        project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        role TEXT NOT NULL CHECK(role IN ('editor','viewer')),
        can_export INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL,
        PRIMARY KEY(project_id,user_id))""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_projects_owner ON projects(owner_user_id,id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id,project_id)")
    # Resolve exact usernames from creation audit events; never guess ownership
    # of token/development projects or silently give old coworkers membership.
    for project_id, in conn.execute("SELECT id FROM projects WHERE owner_user_id IS NULL").fetchall():
        event = conn.execute("SELECT actor FROM audit_logs WHERE project_id=? AND resource_type='project' AND action='create' ORDER BY id LIMIT 1", (project_id,)).fetchone()
        match = re.fullmatch(r"user:([^:]+):[^:]+", event[0]) if event else None
        if match:
            user = conn.execute("SELECT id FROM users WHERE username=?", (match[1],)).fetchone()
            if user:
                conn.execute("UPDATE projects SET owner_user_id=? WHERE id=?", (user[0], project_id))
    selected = os.getenv("MDP_SYSTEM_ADMIN_USERNAME", "").strip().lower()
    if selected:
        admin = conn.execute("SELECT id FROM users WHERE username=? AND enabled=1 AND role='admin'", (selected,)).fetchone()
        if not admin and conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
            raise RuntimeError("MDP_SYSTEM_ADMIN_USERNAME must name an enabled existing administrator")
    else:
        admin = conn.execute("SELECT id FROM users WHERE role='admin' AND enabled=1 ORDER BY created_at,id LIMIT 1").fetchone()
    if admin:
        conn.execute("UPDATE users SET role='user' WHERE id<>?", (admin[0],))
    conn.execute("UPDATE users SET role='user' WHERE role IN ('annotator','reviewer')")
    conn.execute("UPDATE registration_requests SET role='user' WHERE role IN ('annotator','reviewer')")
    if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
        conn.execute("DELETE FROM user_sessions")
        conn.execute("UPDATE assets SET lock_owner=NULL,lock_expires_at=NULL,lock_token=NULL WHERE lock_owner IS NOT NULL")
        conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,payload_json,created_at)
            VALUES ('system','workspace_migration','system','16',NULL,?,?)""",
            (json.dumps({"system_admin_user_id": admin[0] if admin else None}), now))
    if conn.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("Foreign-key verification failed while migrating project ownership")
    conn.execute("INSERT INTO schema_migrations(version,applied_at) VALUES(16,?)", (now,))
