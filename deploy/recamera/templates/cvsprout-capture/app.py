"""Sample original camera frames and deliver them to the paired CVSprout project."""
from datetime import datetime, timezone
from pathlib import Path
import threading
import time
import uuid

import cv2
import numpy as np
from kit.app import App, run_app
from kit.config import appdata_root

from transport import EdgeClient, Spool, Uploader


def encode_original(frame, quality):
    """Encode synchronously while the borrowed frame remains valid.

    Kit CPU frame sources expose full-resolution RGB (or explicitly BGR).
    The owned BGR copy and resulting immutable bytes never borrow DMA storage.
    """
    if frame.fmt not in ("RGB", "BGR"):
        raise ValueError("capture requires an original RGB/BGR Kit frame")
    pixels = np.asarray(frame.data)
    if pixels.shape != (frame.h, frame.w, 3):
        raise ValueError("capture frame is not at original camera resolution")
    bgr = np.array(pixels[..., ::-1] if frame.fmt == "RGB" else pixels,
                   copy=True, order="C")
    ok, result = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, int(quality)])
    if not ok:
        raise RuntimeError("JPEG encoding failed")
    return result.tobytes()


class CVSproutCaptureApp(App):
    id = "cvsprout-capture"
    name = "CVSprout Capture"
    owns_loop = True
    needs_model = False
    model_frame = "cpu"

    def __init__(self):
        super().__init__()
        self.uploader = None
        self.upload_thread = None
        self.capture_count = 0

    def setup(self, config):
        super().setup(config)
        client = EdgeClient(config.get("server_url", ""), config.get("device_token", ""))
        self.jpeg_quality = min(95, max(40, int(config.get("jpeg_quality", 90))))
        self.max_session_images = max(0, int(config.get("max_session_images", 0)))
        self.batch_id = str(uuid.uuid4())
        self.capture_count = 0
        spool = Spool(Path(appdata_root()) / self.id / "captures.sqlite3",
                      max_bytes=min(512, max(16, int(config.get("spool_size_mb", 128)))) * 1024 * 1024,
                      max_images=min(10000, max(10, int(config.get("spool_max_images", 1000)))))
        self.uploader = Uploader(client, spool)

    def run(self):
        self.upload_thread = threading.Thread(target=self.uploader.run,
                                              name="cvsprout-upload", daemon=True)
        self.upload_thread.start()
        next_capture = 0.0
        next_status = 0.0
        try:
            for frame in self.frames():
                if not self.upload_thread.is_alive():
                    raise RuntimeError("capture uploader stopped unexpectedly")
                state = self.uploader.capture_state()
                now = time.monotonic()
                if now >= next_status:
                    stats = self.uploader.spool.stats()
                    self.emit([], frame.pts, results=[], extra={
                        "capture_state": "capturing" if state["enabled"] else "paused",
                        "pending_images": stats["pending_images"],
                        "pending_bytes": stats["pending_bytes"],
                        "uploaded_images": self.uploader.uploaded,
                        "last_error": self.uploader.last_error,
                    })
                    next_status = now + 5
                if not state["enabled"] or now < next_capture:
                    continue
                if self.max_session_images and self.capture_count >= self.max_session_images:
                    continue
                next_capture = now + state["interval"]
                captured_at = datetime.now(timezone.utc).isoformat()
                jpeg = encode_original(frame, self.jpeg_quality)
                if len(jpeg) > state["max_image_bytes"]:
                    self.uploader.last_error = "image_exceeds_server_limit"
                    continue
                capture_id = self.uploader.spool.enqueue(jpeg, state["owner"], captured_at, self.batch_id)
                if capture_id:
                    self.capture_count += 1
                else:
                    self.uploader.last_error = "spool_full_capture_paused"
        finally:
            self._stop_uploader()

    def _stop_uploader(self):
        if self.uploader:
            self.uploader.stop.set()
        if self.upload_thread:
            self.upload_thread.join(timeout=12)
            if self.upload_thread.is_alive():
                raise RuntimeError("capture uploader did not stop within its deadline")
            self.upload_thread = None

    def finish(self):
        try:
            self._stop_uploader()
        finally:
            super().finish()


if __name__ == "__main__":
    run_app(CVSproutCaptureApp())
