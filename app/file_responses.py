"""Pin immutable model downloads independently of their project's lifetime."""
from __future__ import annotations

import errno
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path

import anyio
from fastapi import HTTPException
from starlette.background import BackgroundTask
from starlette.responses import FileResponse

from .config import settings


logger = logging.getLogger(__name__)


def _cleanup_snapshot(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except FileNotFoundError:
        pass
    except OSError:
        logger.warning("Could not remove model download snapshot %s", directory)


class _PinnedFileResponse(FileResponse):
    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # FileResponse does not run its background task after an invalid
            # Range request or a failed/disconnected send. Cleanup is idempotent.
            with anyio.CancelScope(shield=True):
                await self.background()


def pinned_file_response(
    path: Path, *, filename: str, media_type: str = "application/octet-stream"
) -> FileResponse:
    """Snapshot an already validated artifact before ASGI opens its response.

    Hard links cost no model-sized copy and survive deletion of the source.
    Windows and filesystems without hard links use a bounded copy from an open
    file instead; Windows hard links inherit read-only artifact attributes that
    would prevent removing the temporary link.
    """
    directory = None
    response_created = False
    try:
        training_root = settings.data_dir.resolve() / settings.training_dir.name
        downloads = training_root / ".downloads"
        if (settings.training_dir.resolve() != training_root
                or downloads.is_symlink() or downloads.resolve() != downloads):
            raise HTTPException(409, "模型下载临时目录异常，请管理员检查存储配置")
        source = Path(path)
        if source.is_symlink() or not source.resolve().is_relative_to(training_root):
            raise HTTPException(409, "模型产物路径异常，请管理员检查存储配置")
        source = source.resolve()
        source_stat = source.stat()
        if not stat.S_ISREG(source_stat.st_mode):
            raise HTTPException(409, "模型产物不是有效文件")
        downloads.mkdir(parents=True, exist_ok=True)
        if downloads.is_symlink() or downloads.resolve() != downloads:
            raise HTTPException(409, "模型下载临时目录异常，请管理员检查存储配置")
        directory = Path(tempfile.mkdtemp(prefix="artifact-", dir=downloads))
        snapshot = directory / "artifact"
        try:
            if os.name == "nt":
                raise OSError(errno.ENOTSUP, "Copy read-only Windows artifacts")
            os.link(source, snapshot, follow_symlinks=False)
        except OSError as exc:
            if exc.errno not in {errno.EXDEV, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.ENOSYS}:
                raise
            with source.open("rb") as reader, snapshot.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=1024 * 1024)
        response = _PinnedFileResponse(
            snapshot, filename=filename, media_type=media_type,
            stat_result=source_stat,
            background=BackgroundTask(_cleanup_snapshot, directory),
        )
        response_created = True
        return response
    except FileNotFoundError as exc:
        raise HTTPException(409, "模型产物已被删除，请刷新后重试") from exc
    except OSError as exc:
        raise HTTPException(503, "无法准备模型下载，请稍后重试或检查存储空间") from exc
    finally:
        # Successful responses own their cleanup until the download finishes.
        # An exception before construction must not leave partial copies behind.
        if directory is not None and not response_created:
            _cleanup_snapshot(directory)
