FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/src

WORKDIR /app

RUN addgroup --system inspector && adduser --system --ingroup inspector inspector
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --upgrade pip && pip install .

COPY config ./config
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh && mkdir -p /app/data /app/logs && chown -R inspector:inspector /app

EXPOSE 31880
HEALTHCHECK --interval=20s --timeout=5s --start-period=20s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:31880/health/ready', timeout=3)"

FROM base AS test
USER root
RUN pip install '.[dev]'
COPY tests ./tests
USER inspector
ENTRYPOINT []
CMD ["pytest", "-q"]

FROM base AS runtime
USER root
ENTRYPOINT ["./docker-entrypoint.sh"]
