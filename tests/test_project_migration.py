import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class ProjectMigrationTest(unittest.TestCase):
    def setUp(self):
        from app.config import settings
        from app.database import SCHEMA, init_db
        self.settings, self.init_db = settings, init_db
        self.temp = tempfile.TemporaryDirectory(prefix="mdp-workspace-migration-")
        self.old_dir = settings.data_dir
        object.__setattr__(settings, "data_dir", Path(self.temp.name))
        self.addCleanup(self.restore)
        # Start from the actual constrained old schema; init_db must rebuild it
        # without losing IDs, passwords or references from child tables.
        with sqlite3.connect(settings.db_path) as conn:
            conn.executescript(SCHEMA.replace("'admin', 'user',", "'admin',").replace("'admin','user',", "'admin',"))
            conn.executemany("INSERT INTO users(id,username,display_name,password_hash,role,created_at) VALUES (?,?,?,?,?,?)", [
                (1, "root", "Root", "hash-root", "admin", "2020-01-01"),
                (2, "alice", "Alice", "hash-alice", "admin", "2021-01-01"),
                (3, "bob", "Bob", "hash-bob", "annotator", "2022-01-01"),
                (4, "review", "Review", "hash-review", "reviewer", "2022-01-01")])
            conn.executemany("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)", [(10, "Alice", "2022", "2022"), (11, "Legacy token", "2022", "2022")])
            conn.execute("""INSERT INTO audit_logs(actor,action,resource_type,resource_id,project_id,created_at)
                VALUES ('user:alice:session','create','project','10',10,'2022')""")
            conn.execute("INSERT INTO user_sessions(token_hash,user_id,expires_at) VALUES ('old-token',2,'2099')")
            conn.execute("INSERT INTO work_blocks(project_id,name,owner_user_id,created_at,updated_at) VALUES (10,'block',3,'2022','2022')")

    def restore(self):
        object.__setattr__(self.settings, "data_dir", self.old_dir)
        self.temp.cleanup()

    def snapshot(self):
        with sqlite3.connect(self.settings.db_path) as conn:
            return (conn.execute("SELECT id,username,password_hash,role FROM users ORDER BY id").fetchall(),
                    conn.execute("SELECT id,owner_user_id FROM projects ORDER BY id").fetchall())

    def test_legacy_roles_ownership_references_and_idempotency(self):
        with patch.dict(os.environ, {"MDP_SYSTEM_ADMIN_USERNAME": ""}):
            self.init_db()
            users, projects = self.snapshot()
            self.assertEqual([user[3] for user in users], ["admin", "user", "user", "user"])
            self.assertEqual(users[1][:3], (2, "alice", "hash-alice"))
            self.assertEqual(projects, [(10, 2), (11, None)])
            with sqlite3.connect(self.settings.db_path) as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM user_sessions").fetchone()[0], 0)
                self.assertEqual(conn.execute("SELECT owner_user_id FROM work_blocks").fetchone()[0], 3)
                self.assertEqual(conn.execute("PRAGMA foreign_key_check").fetchall(), [])
                conn.execute("INSERT INTO project_members(project_id,user_id,role,created_at) VALUES(10,3,'editor','2023')")
                conn.execute("UPDATE users SET role='admin' WHERE id=3")
            before = self.snapshot()
            self.init_db()
            self.assertEqual(self.snapshot(), before)

    def test_explicit_administrator_selection_and_invalid_selection_rollback(self):
        with patch.dict(os.environ, {"MDP_SYSTEM_ADMIN_USERNAME": "not-found"}):
            with self.assertRaisesRegex(RuntimeError, "MDP_SYSTEM_ADMIN_USERNAME"):
                self.init_db()
        with sqlite3.connect(self.settings.db_path) as conn:
            self.assertFalse(conn.execute("SELECT 1 FROM schema_migrations WHERE version=16").fetchone())
            self.assertEqual(conn.execute("SELECT role FROM users WHERE id=2").fetchone()[0], "admin")
        with patch.dict(os.environ, {"MDP_SYSTEM_ADMIN_USERNAME": "alice"}):
            self.init_db()
        self.assertEqual([user[3] for user in self.snapshot()[0]], ["user", "admin", "user", "user"])
