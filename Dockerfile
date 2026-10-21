FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MDP_DATA_DIR=/data \
    MDP_MODELS_DIR=/models YOLO_CONFIG_DIR=/data/yolo \
    YOLO_AUTOINSTALL=false YOLO_OFFLINE=true
WORKDIR /app
COPY requirements/ ./requirements/
RUN pip install --no-cache-dir -r requirements/web.txt
RUN groupadd --gid 10001 visionforge \
    && useradd --uid 10001 --gid 10001 --create-home visionforge \
    && mkdir -p /data /models/training \
    && chown -R visionforge:visionforge /data /models

FROM scratch AS source
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md /app/
COPY app /app/app
COPY static /app/static
COPY model-catalog /app/model-catalog
COPY scripts /app/scripts
COPY deploy /app/deploy

FROM base AS web
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY --from=source /app /app
USER visionforge
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS compute-base
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

FROM compute-base AS worker-cpu
RUN pip install --no-cache-dir -r requirements/worker-cpu.txt
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY --from=source /app /app
USER visionforge
EXPOSE 8001
CMD ["uvicorn", "app.worker_server:app", "--host", "0.0.0.0", "--port", "8001"]

FROM compute-base AS worker-gpu
RUN pip install --no-cache-dir -r requirements/worker-gpu.txt
RUN python -m pip install --no-cache-dir --upgrade pip==26.2.2
COPY --from=source /app /app
USER visionforge
EXPOSE 8001
CMD ["uvicorn", "app.worker_server:app", "--host", "0.0.0.0", "--port", "8001"]
