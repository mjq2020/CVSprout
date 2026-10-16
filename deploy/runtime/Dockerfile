FROM python:3.12-slim AS base
ENV YOLO_AUTOINSTALL=false YOLO_OFFLINE=true YOLO_CONFIG_DIR=/tmp/yolo PYTHONUNBUFFERED=1
WORKDIR /service
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/*
COPY requirements ./requirements
COPY app ./app
COPY LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
RUN useradd --uid 10001 --create-home visionforge
FROM base AS cpu
RUN pip install --no-cache-dir -r requirements/runtime-cpu.txt
USER visionforge
CMD ["uvicorn", "app.runtime.server:app", "--host", "0.0.0.0", "--port", "8080"]
FROM base AS gpu
RUN pip install --no-cache-dir -r requirements/runtime-gpu.txt
USER visionforge
CMD ["uvicorn", "app.runtime.server:app", "--host", "0.0.0.0", "--port", "8080"]
