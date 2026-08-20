FROM python:3.13-alpine

WORKDIR /app

COPY app.py /app/app.py
COPY patched_app.py /app/patched_app.py
COPY patched_app_v06.py /app/patched_app_v06.py
COPY static /app/static

RUN python3 -m py_compile /app/app.py /app/patched_app.py /app/patched_app_v06.py

ENV PYTHONUNBUFFERED=1 \
    CONFIG_DIR=/config \
    PORT=8781 \
    UPPOLLO_LOG_DIR=/uppollo-logs

VOLUME ["/config", "/uppollo-logs"]
EXPOSE 8781

CMD ["python3", "/app/patched_app_v06.py"]
