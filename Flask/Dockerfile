# Stage 1: builder
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Stage 2: final image — no build tools
FROM python:3.11-slim
COPY --from=builder /install /usr/local
WORKDIR /app
COPY . .
EXPOSE 5000

ENTRYPOINT ["/bin/sh", "-c", "gunicorn --worker-class eventlet -w 1 --bind 0.0.0.0:5000 server:app"]