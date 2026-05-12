# AI Spine-Leaf Network Designer container image.
# Serves Flask on port 10000 (no pywebview in-container).
#
# Docker:
#   docker build -t ainetwork-designer .
#   docker run --rm -p 10000:10000 --name ainetwork-designer ainetwork-designer
# Podman (equivalent):
#   podman build -t ainetwork-designer .
#   podman run --rm -p 10000:10000 --name ainetwork-designer ainetwork-designer
# Open: http://localhost:10000/

FROM python:3.12-slim AS builder

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app:app \
    PATH="/opt/venv/bin:$PATH"

COPY --from=builder /opt/venv /opt/venv
COPY app.py .
COPY templates/ templates/
COPY static/ static/

EXPOSE 10000

CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=10000", "--no-debugger", "--no-reload"]
