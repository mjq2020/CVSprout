"""A standalone, authenticated Ultralytics prediction service."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import io
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import HTMLResponse
from PIL import Image, ImageOps

ROOT = Path(os.getenv("VISIONFORGE_MODEL_DIR", "/model"))
_lock = threading.Lock()
_model = None
_manifest = {}
_key = ""


def authenticate(authorization: str = Header(default="")):
    if not _key or not hmac.compare_digest(authorization, f"Bearer {_key}"):
        raise HTTPException(401, "请提供部署密钥：Authorization: Bearer <key>")


@asynccontextmanager
async def lifespan(app):
    global _model, _manifest, _key
    _key = os.getenv("VISIONFORGE_API_KEY", "")
    if len(_key) < 32:
        raise RuntimeError("VISIONFORGE_API_KEY must contain at least 32 characters; run the bundle startup script.")
    _manifest = json.loads((ROOT / "manifest.json").read_text())
    checkpoint = ROOT / "model.pt"
    with checkpoint.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if checksum != _manifest["sha256"]:
        raise RuntimeError("Model checksum mismatch")
    from ultralytics import YOLO
    _model = YOLO(str(checkpoint), task=_manifest["task"], verbose=False)
    try:
        yield
    finally:
        _model = None


app = FastAPI(title="CVSprout 模型服务", version="1.0", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok" if _model is not None else "loading"}


def _predict(image):
    with _lock:
        result = _model.predict(image, device=os.getenv("VISIONFORGE_DEVICE", "cpu"),
                                imgsz=_manifest["image_size"], verbose=False)[0]
        return {"task": _manifest["task"], "width": image.width, "height": image.height,
                "predictions": result.summary(normalize=False, decimals=4)}


@app.post("/predict", dependencies=[Depends(authenticate)])
async def predict(file: UploadFile = File(...)):
    data = await file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "图片不能超过 20 MB")
    try:
        with Image.open(io.BytesIO(data)) as original:
            if original.width * original.height > 25_000_000:
                raise HTTPException(413, "图片不能超过 2500 万像素")
            image = ImageOps.exif_transpose(original).convert("RGB")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(400, "无法读取图片") from exc
    # Reject overload rather than accumulating unbounded decoded images.
    if not _request_slot.acquire(blocking=False):
        raise HTTPException(429, "模型正在处理其他图片，请稍后重试。")
    task = asyncio.create_task(asyncio.to_thread(_predict, image))
    def done(completed):
        try:
            if not completed.cancelled():
                completed.exception()
        finally:
            _request_slot.release()
    task.add_done_callback(done)
    return await asyncio.shield(task)


_request_slot = threading.Lock()


@app.get("/", response_class=HTMLResponse)
def index():
    return '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>CVSprout 模型测试</title><style>body{font:16px system-ui;max-width:880px;margin:40px auto;padding:0 20px;color:#1f2937}label{display:block;margin:18px 0}input,button{font:inherit;padding:8px}input[type=password]{width:min(100%,540px);box-sizing:border-box}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6fa;padding:16px}canvas{max-width:100%;height:auto}button{cursor:pointer}input:focus,button:focus{outline:2px solid #2563eb}</style>
<h1>测试你的模型</h1><p>上传一张图片，查看预测结果。部署密钥保存在部署目录的 .env 文件中。</p>
<form><label>部署密钥 <input id="key" type="password" required autocomplete="off"></label>
<label>图片 <input id="file" type="file" accept="image/*" required></label><button>运行预测</button></form>
<p id="status" role="status"></p><canvas hidden></canvas><pre id="result" hidden></pre>
<script>document.querySelector('form').onsubmit=async e=>{e.preventDefault();const button=e.target.querySelector('button'),status=document.querySelector('#status'),out=document.querySelector('#result');button.disabled=true;status.textContent='正在预测…';try{const file=document.querySelector('#file').files[0],body=new FormData();body.append('file',file);const response=await fetch('/predict',{method:'POST',headers:{Authorization:'Bearer '+document.querySelector('#key').value},body});const data=await response.json();if(!response.ok)throw Error(typeof data.detail==='string'?data.detail:'预测请求失败');out.hidden=false;out.textContent=JSON.stringify(data,null,2);const image=await createImageBitmap(file),canvas=document.querySelector('canvas');canvas.width=image.width;canvas.height=image.height;canvas.hidden=false;const ctx=canvas.getContext('2d');ctx.drawImage(image,0,0);ctx.strokeStyle='#16a34a';ctx.fillStyle='#16a34a';ctx.lineWidth=Math.max(2,image.width/400);ctx.font=Math.max(14,image.width/50)+'px sans-serif';for(const p of data.predictions){if(p.box){const b=p.box;ctx.strokeRect(b.x1,b.y1,b.x2-b.x1,b.y2-b.y1);ctx.fillText(p.name,b.x1,Math.max(20,b.y1-5));}}image.close();status.textContent='预测完成';}catch(error){status.textContent=error.message;}finally{button.disabled=false;}};</script></html>'''
