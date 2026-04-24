# AI Spine-Leaf Network Designer — listens on 6000 inside the container.
# Run:  docker build -t ainetwork-designer .
#       docker run --rm -p 6000:6000 ainetwork-designer
# Then open http://localhost:6000/

FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    FLASK_APP=app:app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates/ templates/

EXPOSE 6000

CMD ["python", "-m", "flask", "run", "--host=0.0.0.0", "--port=6000", "--no-debug"]
