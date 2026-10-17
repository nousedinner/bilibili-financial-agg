FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
RUN mkdir -p /app/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY requirements-test.txt .
RUN pip install --no-cache-dir -r requirements-test.txt

COPY src/ ./src/
COPY scripts/ ./scripts/
COPY tests/ ./tests/
COPY config/config.example.yaml ./config/config.example.yaml

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]
