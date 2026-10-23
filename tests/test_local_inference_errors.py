"""Controlled local model errors survive both embedded and worker execution."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException


class LocalInferenceErrorsTest(unittest.TestCase):
    def test_preview_returns_actionable_qwen_error(self):
        from app.annotation_preview import run_preview
        from app.providers.qwen_local import QwenLocalError

        async def check():
            with patch("app.annotation_preview.local_inference_busy", return_value=False), \
                    patch("app.annotation_preview._preview_images", new=AsyncMock(side_effect=QwenLocalError("显存不足，请选择较小模型"))):
                with self.assertRaises(HTTPException) as caught:
                    await run_preview({}, {"provider_type": "local_qwen"}, None, [], {}, "", [])
                self.assertEqual(caught.exception.status_code, 422)
                self.assertIn("显存不足", caught.exception.detail)
            await asyncio.sleep(0)
        asyncio.run(check())

    def test_worker_marks_only_controlled_inference_errors(self):
        from app.config import settings
        from app.providers.qwen_local import QwenLocalError
        from app.worker_server import Predict, predict

        previous = settings.data_dir
        with tempfile.TemporaryDirectory(prefix="cvsprout-local-error-") as directory:
            object.__setattr__(settings, "data_dir", Path(directory))
            try:
                settings.media_dir.mkdir(parents=True)
                (settings.media_dir / "fixture.png").write_bytes(b"unused")
                provider = AsyncMock()
                provider.predict.side_effect = QwenLocalError("权重不完整，请重新准备")
                body = Predict(model_id="qwen3.5-0.8b", image="fixture.png", width=1, height=1)
                with patch("app.providers.get_provider", return_value=provider):
                    with self.assertRaises(HTTPException) as caught:
                        asyncio.run(predict(body))
                self.assertEqual(caught.exception.status_code, 422)
                self.assertEqual(caught.exception.detail, {"code": "local_model_inference", "message": "权重不完整，请重新准备"})
            finally:
                object.__setattr__(settings, "data_dir", previous)

    def test_worker_proxy_exposes_only_controlled_messages(self):
        from app.worker_client import _result

        response = httpx.Response(422, json={"detail": {"code": "local_model_inference", "message": "内存不足"}})
        with self.assertRaises(HTTPException) as caught:
            _result(response)
        self.assertEqual((caught.exception.status_code, caught.exception.detail), (422, "内存不足"))
        for detail in ("private server failure", [{"input": "secret"}], {"code": "unexpected", "message": "secret"}):
            with self.assertRaises(HTTPException) as caught:
                _result(httpx.Response(422, json={"detail": detail}))
            self.assertEqual(caught.exception.status_code, 503)
            self.assertNotIn("secret", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
