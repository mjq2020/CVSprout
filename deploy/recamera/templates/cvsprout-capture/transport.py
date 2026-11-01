"""Bounded, restart-safe capture delivery. No camera or native SDK imports."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


MAX_IMAGE_BYTES = 10 * 1024 * 1024
# JPEG quotas exclude SQLite pages/indexes. Bound the physical main database
# independently; the manifest reserves 1152 MiB for this file plus a worst-case
# DELETE rollback journal (one original copy of each modified page + headers).
MAX_DATABASE_BYTES = 560 * 1024 * 1024


class DeliveryError(RuntimeError):
    """A sanitized failure: response bodies, URLs and credentials stay private."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise DeliveryError("server_redirect_refused")


def normalize_server(value):
    url = urllib.parse.urlsplit(str(value).strip())
    if (url.scheme not in ("http", "https") or not url.hostname
            or url.username or url.password or url.query or url.fragment):
        raise ValueError("server_url must be an HTTP(S) origin or base path")
    try:
        url.port
    except ValueError as exc:
        raise ValueError("server_url has an invalid port") from exc
    return urllib.parse.urlunsplit((url.scheme, url.netloc, url.path.rstrip("/"), "", ""))


class EdgeClient:
    def __init__(self, server_url, token, timeout=3):
        self.server_url = normalize_server(server_url)
        self._token = str(token).strip()
        if not self._token or any(c in self._token for c in "\r\n"):
            raise ValueError("device_token is required")
        self.timeout = timeout
        # Explicitly avoid environment proxies on a device; they may route a
        # local camera credential to an unrelated proxy. TLS stays verified.
        self._opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, path, payload=None, content_type="application/json"):
        headers = {"Authorization": "Bearer " + self._token,
                   "Accept": "application/json", "User-Agent": "CVSprout-Capture/1.0.0"}
        if payload is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(self.server_url + "/api/edge/v1/" + path,
                                     data=payload, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                data = response.read(65537)
                if len(data) > 65536:
                    raise DeliveryError("server_response_too_large")
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise DeliveryError("invalid_server_response")
                return result
        except urllib.error.HTTPError as exc:
            raise DeliveryError("http_%s" % exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            raise DeliveryError("server_unreachable_or_invalid_response") from None

    def configuration(self):
        return self.request("config")

    def heartbeat(self, values):
        return self.request("heartbeat", json.dumps(values).encode())

    def upload(self, capture):
        boundary = "cvsprout-" + uuid.uuid4().hex
        chunks = []
        for key in ("capture_id", "captured_at", "batch_id", "model_release"):
            value = capture.get(key)
            if value is not None:
                chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\""
                               "\r\n\r\n%s\r\n" % (boundary, key, value)).encode())
        chunks.extend([
            ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
             "filename=\"capture.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n" % boundary).encode(),
            capture["jpeg"], ("\r\n--%s--\r\n" % boundary).encode()])
        return self.request("captures", b"".join(chunks),
                            "multipart/form-data; boundary=" + boundary)


class Spool:
    """One transactional database stores each image and its immutable receipt ID.

    FULL synchronous commits survive process/device restart. Capacity pressure
    rejects new captures; it never drops images that lack a server receipt.
    """
    def __init__(self, path, max_bytes=128 * 1024 * 1024, max_images=1000):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.max_bytes = int(max_bytes)
        self.max_images = int(max_images)
        if self.max_bytes < 1 or self.max_images < 1:
            raise ValueError("spool limits must be positive")
        with self._connect() as conn:
            journal = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
            if journal.lower() != "delete":
                raise RuntimeError("capture queue requires DELETE journaling")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS captures (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    capture_id TEXT NOT NULL UNIQUE,
                    owner TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    batch_id TEXT,
                    model_release TEXT,
                    jpeg BLOB NOT NULL
                );
                CREATE INDEX IF NOT EXISTS captures_owner ON captures(owner, sequence);
            """)
        os.chmod(self.path, 0o600)

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA synchronous=FULL")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            page_limit = MAX_DATABASE_BYTES // page_size
            actual_limit = conn.execute("PRAGMA max_page_count=%d" % page_limit).fetchone()[0]
            if actual_limit > page_limit:
                raise RuntimeError("existing capture queue exceeds its physical storage limit")
            with conn:
                yield conn
        finally:
            conn.close()

    def stats(self):
        with self._connect() as conn:
            row = conn.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(jpeg)),0) FROM captures").fetchone()
            return {"pending_images": row[0], "pending_bytes": row[1]}

    def enqueue(self, jpeg, owner, captured_at, batch_id=None, model_release=None):
        if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > MAX_IMAGE_BYTES:
            raise ValueError("invalid JPEG payload size")
        capture_id = str(uuid.uuid4())
        try:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                count, size = conn.execute("SELECT COUNT(*),COALESCE(SUM(LENGTH(jpeg)),0) FROM captures").fetchone()
                if count >= self.max_images or size + len(jpeg) > self.max_bytes:
                    return None
                conn.execute("INSERT INTO captures(capture_id,owner,captured_at,batch_id,model_release,jpeg) "
                             "VALUES(?,?,?,?,?,?)", (capture_id, owner, captured_at, batch_id, model_release, jpeg))
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                # The failed transaction rolls back. Preserve existing entries
                # and let the app pause sampling while the uploader drains them.
                return None
            raise
        return capture_id

    def oldest(self, owner):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM captures WHERE owner=? ORDER BY sequence LIMIT 1", (owner,)).fetchone()
            return dict(row) if row else None

    def acknowledge(self, capture, receipt):
        if (not isinstance(receipt, dict) or receipt.get("received") is not True
                or receipt.get("capture_id") != capture["capture_id"]
                or type(receipt.get("asset_id")) is not int or receipt["asset_id"] <= 0):
            raise DeliveryError("capture_acknowledgement_mismatch")
        try:
            with self._connect() as conn:
                conn.execute("DELETE FROM captures WHERE capture_id=? AND owner=?",
                             (capture["capture_id"], capture["owner"]))
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL:
                # Even an acknowledged entry needs journal space to remove.
                # Retain it and replay the same ID after storage recovers.
                raise DeliveryError("spool_storage_full") from None
            raise


class Uploader:
    def __init__(self, client, spool, clock=time.monotonic):
        self.client, self.spool, self.clock = client, spool, clock
        self.local_max_bytes, self.local_max_images = spool.max_bytes, spool.max_images
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.owner = None
        self.enabled = False
        self.interval = 10.0
        self.max_image_bytes = MAX_IMAGE_BYTES
        self.valid_until = 0.0
        self.next_config = self.next_heartbeat = self.next_upload = 0.0
        self.failures = 0
        self.uploaded = 0
        self.last_error = ""

    def capture_state(self):
        with self.lock:
            return {"owner": self.owner,
                    "enabled": self.enabled and self.clock() < self.valid_until,
                    "interval": self.interval, "max_image_bytes": self.max_image_bytes}

    def _configuration(self, now):
        config = self.client.configuration()
        device, project = config.get("device_id"), config.get("project_id")
        if not isinstance(device, (str, int)) or not device or type(project) is not int or project <= 0:
            raise DeliveryError("invalid_device_configuration")
        interval = config.get("capture_interval_seconds", 10)
        if type(interval) not in (int, float) or not math.isfinite(interval) or not 1 <= interval <= 86400:
            raise DeliveryError("invalid_capture_interval")
        limit = config.get("max_image_bytes", MAX_IMAGE_BYTES)
        if type(limit) is not int or limit <= 0:
            raise DeliveryError("invalid_capture_size_limit")
        pending_bytes = config.get("max_pending_bytes", self.local_max_bytes)
        pending_images = config.get("max_pending_images", self.local_max_images)
        if (type(pending_bytes) is not int or pending_bytes <= 0
                or type(pending_images) is not int or pending_images <= 0):
            raise DeliveryError("invalid_capture_queue_limit")
        owner = hashlib.sha256(json.dumps([self.client.server_url, str(device), project]).encode()).hexdigest()
        with self.lock:
            self.owner = owner
            self.enabled = config.get("capture_enabled") is True and not config.get("project_archived", False)
            self.interval = float(interval)
            self.max_image_bytes = min(limit, MAX_IMAGE_BYTES)
            self.spool.max_bytes = min(pending_bytes, self.local_max_bytes)
            self.spool.max_images = min(pending_images, self.local_max_images)
            # An offline device can spool briefly; it cannot ignore an unseen
            # pause or revocation forever. Restart always requires a handshake.
            self.valid_until = now + 300
        self.next_config = now + 30

    def step(self):
        now = self.clock()
        if now >= self.next_config:
            try:
                self._configuration(now)
            except DeliveryError as exc:
                self.last_error = str(exc)
                self.next_config = now + 15
                if self.last_error in ("http_401", "http_403", "http_404", "http_410"):
                    with self.lock:
                        self.enabled = False
                        self.valid_until = 0
        if self.stop.is_set():
            return 1
        state = self.capture_state()
        if state["owner"] and now >= self.next_heartbeat:
            try:
                self.client.heartbeat({"app_version": "1.0.0", "state": "capturing" if state["enabled"] else "paused",
                                       **self.spool.stats(), "uploaded_images": self.uploaded,
                                       "last_error": self.last_error[:120]})
            except DeliveryError as exc:
                self.last_error = str(exc)
            self.next_heartbeat = now + 30
        if self.stop.is_set() or not state["owner"] or now < self.next_upload:
            return 1
        capture = self.spool.oldest(state["owner"])
        if capture:
            try:
                receipt = self.client.upload(capture)
                self.spool.acknowledge(capture, receipt)
                self.failures = 0
                self.uploaded += 1
                self.last_error = ""
                self.next_upload = now + 0.1
            except DeliveryError as exc:
                self.failures += 1
                self.last_error = str(exc)
                self.next_upload = now + min(60, 2 ** min(self.failures, 6))
                if self.last_error in ("http_401", "http_403", "http_404", "http_410"):
                    with self.lock:
                        self.enabled = False
                        self.valid_until = 0
                    self.next_config = min(self.next_config, now + 15)
        return 0.1 if capture and not self.failures else 1

    def run(self):
        while not self.stop.is_set():
            self.stop.wait(self.step())
