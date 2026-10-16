"""Local compute service. Never publish this port to the host or the internet."""
from __future__ import annotations

import asyncio
import hmac
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from .config import settings
from .worker_client import worker_token


def authenticate(x_visionforge_worker: str = Header(default="")):
    if not hmac.compare_digest(x_visionforge_worker, worker_token()):
        raise HTTPException(401, "Invalid worker credential")


@asynccontextmanager
async def lifespan(app):
    if settings.worker_url:
        raise RuntimeError("Worker must not proxy to another worker")
    from .database import init_db
    from .providers import seed_builtin_models
    from .jobs import JobWorker
    from .training import PipelineWorker
    settings.validate()
    init_db()
    worker_token()
    seed_builtin_models()
    workers = [JobWorker(), PipelineWorker("compute")]
    tasks = [asyncio.create_task(worker.run_forever()) for worker in workers]
    try:
        yield
    finally:
        for worker in workers:
            worker.stop()
        for task in tasks:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=10)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task


app = FastAPI(title="CVSprout Compute", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.get("/health")
def health():
    return {"status": "ok"}


class Call(BaseModel):
    args: list = Field(default_factory=list)
    kwargs: dict = Field(default_factory=dict)


@app.post("/internal/call/{operation}", dependencies=[Depends(authenticate)])
def invoke(operation: str, body: Call):
    from . import training
    from .providers.local import local_runtime_status, local_sam3_runtime_status
    functions = {
        "backend": training._backend_info,
        "models": training.model_catalog,
        "resources": training.training_resources,
        "availability": training.training_availability,
        "checkpoint": training._checkpoint_probe,
        "local-status": local_runtime_status,
        "sam3-status": local_sam3_runtime_status,
    }
    function = functions.get(operation)
    if function is None:
        raise HTTPException(404, "Unknown operation")
    if operation == "checkpoint":
        from pathlib import Path
        path = Path(body.args[0]).resolve()
        if path.parent not in {directory.resolve() for directory in settings.training_model_dirs}:
            raise HTTPException(400, "Checkpoint outside configured model directories")
        body.args[0] = path
    return function(*body.args, **body.kwargs)


class Predict(BaseModel):
    model_id: str
    image: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    prompt: str = ""
    labels: list[str] = Field(default_factory=list)
    hints: dict | None = None


@app.post("/internal/predict", dependencies=[Depends(authenticate)])
async def predict(body: Predict):
    from .providers import get_provider
    from .training import ResourceBusy
    root = settings.media_dir.resolve()
    path = (root / body.image).resolve()
    if root not in path.parents or not path.is_file():
        raise HTTPException(400, "Image outside media storage or missing")
    provider = get_provider(body.model_id)
    if provider is None:
        raise HTTPException(404, "Model unavailable")
    try:
        result = await provider.predict(path, body.width, body.height, body.prompt, body.labels, body.hints)
        return [asdict(prediction) for prediction in result]
    except ResourceBusy as exc:
        raise HTTPException(409, str(exc)) from exc
