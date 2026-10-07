FROM python:3.12-alpine

WORKDIR /app
COPY app.py /app/app.py

ENV PORT=8080 \
    BIND_HOST=0.0.0.0 \
    EVENTS_FILE=/data/player-events.log \
    CACHE_SECONDS=2 \
    PYTHONUNBUFFERED=1

EXPOSE 8080
USER nobody
CMD ["python", "/app/app.py"]
