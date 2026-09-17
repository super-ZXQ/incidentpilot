FROM python:3.12-slim

RUN pip install --no-cache-dir \
    pytest==8.4.2 \
    fastapi==0.118.0 \
    httpx==0.28.1 \
    sqlalchemy==2.0.43 \
    prometheus-client==0.22.1 \
    pydantic==2.11.9

USER 65534:65534
WORKDIR /workspace
ENTRYPOINT []
