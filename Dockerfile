FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY repost_bot ./repost_bot

RUN useradd --create-home --uid 10001 bot \
    && mkdir -p /app/data /app/logs \
    && chown -R bot:bot /app/data /app/logs
USER bot

ENV DB_PATH=/app/data/repost.db \
    LOG_FILE=/app/logs/repost-bot.log \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8080

EXPOSE 8080
VOLUME ["/app/data", "/app/logs"]
CMD ["python", "-m", "repost_bot"]
