# stage 1: builder
FROM python:3.13-slim AS builder

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir build && \
    python -m build --wheel --outdir /app/dist

# stage 2: runtime
FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/dist/*.whl /tmp/

RUN pip install --no-cache-dir /tmp/*.whl && \
    rm -f /tmp/*.whl

# pre-download the default embedding model so first run is fast
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('multi-qa-MiniLM-L6-cos-v1')"

COPY .env.example /app/.env.example

ENV SYNAPTO_PG_DSN=postgresql://synapto:synapto@postgres:5432/synapto
ENV SYNAPTO_REDIS_URL=redis://redis:6379/0

# optional SSE transport
EXPOSE 8080

ENTRYPOINT ["synapto"]
CMD ["serve"]
