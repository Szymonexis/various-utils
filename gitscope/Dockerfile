FROM python:3.12-slim

# git is the whole point; nothing else needed
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# Mounted repos are owned by a different uid than the container user, and git refuses to
# read them by default ("dubious ownership"). This is a local tool, so trust every mount.
RUN git config --system --add safe.directory '*'

WORKDIR /app
COPY backend/requirements.txt backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend backend
COPY frontend frontend

ENV REPOS_ROOT=/repos \
    DATA_DIR=/data \
    PYTHONUNBUFFERED=1

EXPOSE 8080
VOLUME ["/data"]

WORKDIR /app/backend
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
