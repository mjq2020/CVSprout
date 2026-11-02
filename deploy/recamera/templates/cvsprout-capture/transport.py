"""Bounded, restart-safe capture delivery. No camera or native SDK imports."""
from __future__ import annotations

import hashlib
import errno
import json
import math
import os
from pathlib import Path
import stat
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


APP_VERSION = "1.0.1"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_SPOOL_BYTES = 512 * 1024 * 1024
MAX_SPOOL_IMAGES = 10000
MAX_HEADER_BYTES = 2048


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
                   "Accept": "application/json", "User-Agent": "CVSprout-Capture/" + APP_VERSION}
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
    """A bounded, single-writer file queue, with one atomic record per capture.

    AppMgr permits one app instance; a lock serializes its sampling and upload
    threads. An index rebuilt on startup avoids scanning thousands of files on
    every frame. Each immutable record contains metadata and JPEG bytes together,
    so a crash cannot leave a published image without its delivery identity.
    """
    def __init__(self, path, max_bytes=128 * 1024 * 1024, max_images=1000):
        self.path = Path(path)
        self.max_bytes = min(int(max_bytes), MAX_SPOOL_BYTES)
        self.max_images = min(int(max_images), MAX_SPOOL_IMAGES)
        if self.max_bytes < 1 or self.max_images < 1:
            raise ValueError("spool limits must be positive")
        self.lock = threading.RLock()
        self._records = {}
        self._bytes = 0
        self._sequence = 0
        if self.path.is_symlink():
            raise DeliveryError("capture_spool_invalid_directory")
        missing_directories = []
        ancestor = self.path
        while not ancestor.exists():
            missing_directories.append(ancestor)
            ancestor = ancestor.parent
        self.path.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in missing_directories:
            os.chmod(directory, 0o700)
        os.chmod(self.path, 0o700)
        temporary = []
        for entry in self.path.iterdir():
            if (entry.name.startswith(".pending-") and entry.name.endswith(".tmp")
                    and len(entry.name) == 45
                    and all(c in "0123456789abcdef" for c in entry.name[9:-4])):
                if not stat.S_ISREG(entry.lstat().st_mode):
                    raise DeliveryError("capture_spool_corrupt")
                temporary.append(entry)
                continue
            record = self._read_record(entry, payload=False)
            capture_id = record["capture_id"]
            if capture_id in self._records:
                raise DeliveryError("capture_spool_corrupt")
            self._records[capture_id] = (entry, record)
            self._bytes += record["size"]
            self._sequence = max(self._sequence, record["sequence"])
            os.chmod(entry, 0o600)
        if self._bytes > MAX_SPOOL_BYTES or len(self._records) > MAX_SPOOL_IMAGES:
            raise DeliveryError("capture_spool_exceeds_storage_limit")
        # Only unpublished temporary records are discarded. Any corrupt
        # published record above stops startup and leaves every file intact.
        for entry in temporary:
            entry.unlink()
        self._sync_directory()
        # On first launch both appdata/<id> and capture-spool can be new.
        # Persist each new directory's entry in its parent, up to the existing
        # ancestor; syncing only capture-spool's immediate parent is insufficient.
        for directory in missing_directories:
            parent_fd = os.open(directory.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)

    def _sync_directory(self):
        fd = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _text(value, maximum, optional=False):
        return ((optional and value is None) or
                (isinstance(value, str) and 0 < len(value) <= maximum
                 and all(ord(char) >= 32 and ord(char) != 127 for char in value)))

    @staticmethod
    def _filename(record):
        return "%020d-%s.capture" % (record["sequence"], record["capture_id"])

    def _read_record(self, path, payload):
        try:
            if not stat.S_ISREG(path.lstat().st_mode):
                raise ValueError("record is not a regular file")
            with path.open("rb") as stream:
                header = stream.readline(MAX_HEADER_BYTES + 1)
                if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n"):
                    raise ValueError("invalid record header")
                record = json.loads(header)
                if (not isinstance(record, dict)
                        or set(record) != {"version", "sequence", "capture_id", "owner", "captured_at",
                                          "batch_id", "model_release", "size", "sha256"}
                        or type(record["version"]) is not int or record["version"] != 1
                        or type(record["sequence"]) is not int or not 0 < record["sequence"] < 10 ** 20
                        or not isinstance(record["capture_id"], str)
                        or str(uuid.UUID(record["capture_id"])) != record["capture_id"]
                        or not self._text(record["owner"], 128)
                        or not self._text(record["captured_at"], 64)
                        or not self._text(record["batch_id"], 128, optional=True)
                        or not self._text(record["model_release"], 128, optional=True)
                        or type(record["size"]) is not int or not 0 < record["size"] <= MAX_IMAGE_BYTES
                        or not isinstance(record["sha256"], str) or len(record["sha256"]) != 64
                        or any(c not in "0123456789abcdef" for c in record["sha256"])
                        or self._filename(record) != path.name
                        or os.fstat(stream.fileno()).st_size != len(header) + record["size"]):
                    raise ValueError("invalid record metadata")
                if payload:
                    jpeg = stream.read(record["size"] + 1)
                    if len(jpeg) != record["size"] or hashlib.sha256(jpeg).hexdigest() != record["sha256"]:
                        raise ValueError("invalid record payload")
                    return record, jpeg
                return record
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            raise DeliveryError("capture_spool_corrupt") from None

    def stats(self):
        with self.lock:
            return {"pending_images": len(self._records), "pending_bytes": self._bytes}

    def enqueue(self, jpeg, owner, captured_at, batch_id=None, model_release=None):
        if not isinstance(jpeg, bytes) or not jpeg or len(jpeg) > MAX_IMAGE_BYTES:
            raise ValueError("invalid JPEG payload size")
        if (not self._text(owner, 128) or not self._text(captured_at, 64)
                or not self._text(batch_id, 128, optional=True)
                or not self._text(model_release, 128, optional=True)):
            raise ValueError("invalid capture metadata")
        with self.lock:
            if (len(self._records) >= min(self.max_images, MAX_SPOOL_IMAGES)
                    or self._bytes + len(jpeg) > min(self.max_bytes, MAX_SPOOL_BYTES)):
                return None
            capture_id = str(uuid.uuid4())
            self._sequence += 1
            record = {"version": 1, "sequence": self._sequence, "capture_id": capture_id,
                      "owner": owner, "captured_at": captured_at, "batch_id": batch_id,
                      "model_release": model_release, "size": len(jpeg),
                      "sha256": hashlib.sha256(jpeg).hexdigest()}
            header = json.dumps(record, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(header) > MAX_HEADER_BYTES:
                raise ValueError("capture metadata is too large")
            temporary = self.path / (".pending-" + uuid.uuid4().hex + ".tmp")
            destination = self.path / self._filename(record)
            published = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(header)
                    stream.write(jpeg)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                published = True
                # Track the record before syncing the directory: if fsync fails,
                # it still occupies quota and must never become an orphan.
                self._records[capture_id] = (destination, record)
                self._bytes += len(jpeg)
                self._sync_directory()
            except OSError as exc:
                if not published:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        # Restart cleans a bounded unpublished temp record.
                        raise DeliveryError("spool_storage_error") from None
                if not published and exc.errno in (errno.ENOSPC, errno.EDQUOT):
                    return None
                # A published record remains tracked, but durability is not
                # confirmed if directory sync failed. Stop this sampling run
                # instead of reporting a rejected frame and bypassing the
                # finite session capture quota. Restart recovers this same ID.
                raise DeliveryError("spool_storage_error") from None
            return capture_id

    def oldest(self, owner):
        with self.lock:
            candidates = [item for item in self._records.values() if item[1]["owner"] == owner]
            if not candidates:
                return None
            path, indexed = min(candidates, key=lambda item: item[1]["sequence"])
            record, jpeg = self._read_record(path, payload=True)
            if record != indexed:
                raise DeliveryError("capture_spool_corrupt")
            return {**record, "jpeg": jpeg}

    def acknowledge(self, capture, receipt):
        if (not isinstance(receipt, dict) or receipt.get("received") is not True
                or receipt.get("capture_id") != capture["capture_id"]
                or type(receipt.get("asset_id")) is not int or receipt["asset_id"] <= 0):
            raise DeliveryError("capture_acknowledgement_mismatch")
        with self.lock:
            item = self._records.get(capture["capture_id"])
            if item is None:
                return  # a repeated ACK is harmless
            path, indexed = item
            if capture["owner"] != indexed["owner"]:
                raise DeliveryError("capture_acknowledgement_mismatch")
            if self._read_record(path, payload=False) != indexed:
                raise DeliveryError("capture_spool_corrupt")
            try:
                path.unlink()
                self._records.pop(capture["capture_id"])
                self._bytes -= indexed["size"]
                self._sync_directory()
            except OSError as exc:
                # A failed unlink preserves the entry. A failed directory sync
                # after unlink may replay the same server-acknowledged ID after
                # power loss, which the server accepts idempotently.
                raise DeliveryError("spool_storage_full" if exc.errno in (errno.ENOSPC, errno.EDQUOT)
                                    else "spool_storage_error") from None


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
                self.client.heartbeat({"app_version": APP_VERSION, "state": "capturing" if state["enabled"] else "paused",
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
