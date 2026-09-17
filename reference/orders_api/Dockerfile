FROM python:3.12-slim

RUN pip install --no-cache-dir \
    fastapi==0.118.0 \
    uvicorn==0.37.0 \
    sqlalchemy==2.0.43 \
    psycopg[binary]==3.2.10 \
    prometheus-client==0.22.1 \
    pydantic==2.11.9

WORKDIR /app
COPY . /app
RUN mkdir -p /app/var/logs && chown -R 65534:65534 /app
USER 65534:65534
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8001"]
