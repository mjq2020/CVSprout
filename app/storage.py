from __future__ import annotations

import asyncio
import io
import hashlib
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from fastapi import HTTPException, UploadFile
from PIL import Image, UnidentifiedImageError

from .config import settings


ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
FORMAT_DETAILS = {
    "jpeg": (".jpg", "image/jpeg"),
    "png": (".png", "image/png"),
    "webp": (".webp", "image/webp"),
    "bmp": (".bmp", "image/bmp"),
}


@dataclass
class StoredImage:
    filename: str
    storage_name: str
    mime_type: str
    width: int
    height: int
    size_bytes: int
    sha256: str


def _store_image(filename: str, content: bytes) -> StoredImage:
    if not content:
        raise ValueError("empty image")
    try:
        with Image.open(io.BytesIO(content)) as image:
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            width, height = image.size
            image_format = (image.format or "").lower()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("unsupported or damaged image") from exc

    if image_format not in FORMAT_DETAILS:
        raise ValueError("unsupported image format")
    if width <= 0 or height <= 0 or width * height > settings.max_image_pixels:
        raise ValueError(f"image exceeds {settings.max_image_pixels} pixels")
    ext, mime = FORMAT_DETAILS[image_format]
    storage_name = f"{uuid.uuid4().hex}{ext}"
    (settings.media_dir / storage_name).write_bytes(content)
    return StoredImage(
        Path(filename).name,
        storage_name,
        mime,
        width,
        height,
        len(content),
        hashlib.sha256(content).hexdigest(),
    )


def _unpack_content(filename: str, content: bytes) -> list[StoredImage]:
    max_bytes = settings.max_upload_mb * 1024 * 1024
    if len(content) > max_bytes:
        raise HTTPException(413, f"单个上传文件不能超过 {settings.max_upload_mb} MB")

    if Path(filename).suffix.lower() != ".zip":
        try:
            return [_store_image(filename, content)]
        except ValueError as exc:
            raise HTTPException(415, f"{filename}: {exc}") from exc

    images: list[StoredImage] = []
    extracted_bytes = 0
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                if info.is_dir() or path.suffix.lower() not in ALLOWED_EXTENSIONS:
                    continue
                if info.file_size > max_bytes or info.file_size <= 0:
                    continue
                if len(images) >= settings.max_assets_per_upload:
                    raise HTTPException(
                        413, f"单次上传图片数量不能超过 {settings.max_assets_per_upload}"
                    )
                extracted_bytes += info.file_size
                if extracted_bytes > max_bytes:
                    raise HTTPException(413, f"ZIP 解压后的图片总量不能超过 {settings.max_upload_mb} MB")
                try:
                    images.append(_store_image(path.name, archive.read(info)))
                except ValueError:
                    continue
    except zipfile.BadZipFile as exc:
        for image in images:
            (settings.media_dir / image.storage_name).unlink(missing_ok=True)
        raise HTTPException(415, "ZIP 文件损坏") from exc
    except Exception:
        # A ZIP may already have staged valid images before a later entry trips a
        # resource limit. Do not leave those unreferenced files behind.
        for image in images:
            (settings.media_dir / image.storage_name).unlink(missing_ok=True)
        raise
    if not images:
        raise HTTPException(415, "ZIP 中没有可用图片")
    return images


async def unpack_upload(upload: UploadFile) -> list[StoredImage]:
    content = await upload.read()
    filename = upload.filename or "upload"
    # Pillow verification, ZIP decompression, hashing and filesystem writes are
    # blocking work. Running them outside the ASGI event loop lets multiple small
    # upload batches make progress without freezing job polling and other API calls.
    return await asyncio.to_thread(_unpack_content, filename, content)
