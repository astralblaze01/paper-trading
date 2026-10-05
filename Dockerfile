FROM python:3.12-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home app
COPY app ./app
COPY tests ./tests
COPY scripts ./scripts
USER app
EXPOSE 8000
# uvicorn starts WEB_CONCURRENCY processes. More than one needs Redis, where rate
# limits, the ranking snapshot and SSE connection counts are shared.
ENV WEB_CONCURRENCY=1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
