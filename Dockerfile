# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates \
    build-essential gcc libssl-dev libffi-dev \
  && rm -rf /var/lib/apt/lists/*

# Install Python deps first (better layer caching)
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app and run script
COPY app ./app
COPY .env.example ./.env.example
COPY run.sh ./run.sh
RUN chmod +x ./run.sh

# Expose port
EXPOSE 8000

# Run
CMD ["./run.sh"]
