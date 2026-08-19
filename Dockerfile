FROM python:3.13-alpine

WORKDIR /app

COPY app.py /app/app.py
COPY static /app/static

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    PORT=8781 \
    UPPOLLO_LOG_DIR=/uppollo-logs

VOLUME ["/config", "/uppollo-logs"]
EXPOSE 8781

CMD ["python3", "/app/app.py"]
