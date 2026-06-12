FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
EXPOSE 5001

ENTRYPOINT ["gunicorn", "-w", "2", "--bind", "0.0.0.0:5001", "uplink_server:app"]
