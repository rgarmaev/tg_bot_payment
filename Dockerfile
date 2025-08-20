# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
WORKDIR /app

# No system deps required; keep image slim to avoid cache space issues

# Install Python deps first (better layer caching)
COPY requirements.txt ./
RUN python -m pip install --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy app and run script
COPY app ./app
COPY run.sh ./run.sh
RUN chmod +x ./run.sh

# Expose port
EXPOSE 8000

# Run
CMD ["./run.sh"]
