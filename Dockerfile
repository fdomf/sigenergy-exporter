FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml constraints.txt README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir --constraint constraints.txt .

COPY sigenergy.yml /etc/sigenergy-exporter/sigenergy.yml

FROM base AS test

COPY sigenergy.yml ./sigenergy.yml
COPY tests ./tests
CMD ["python", "-m", "unittest", "discover", "-v", "tests"]

FROM base AS runtime

LABEL org.opencontainers.image.title="sigenergy-exporter" \
      org.opencontainers.image.description="Read-only Prometheus exporter for Sigenergy Modbus TCP targets" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.authors="Francesc Domene" \
      org.opencontainers.image.source="https://github.com/fdomf/sigenergy-exporter"

USER 65534:65534
EXPOSE 10047

ENTRYPOINT ["sigenergy-exporter", "--config.file=/etc/sigenergy-exporter/sigenergy.yml"]
