# One Railway service; the local Bot API server and Python bot share the same files.
# Pinned major Python version; use a fixed digest for the aiogram image in production.
FROM aiogram/telegram-bot-api:latest AS telegram-api
FROM python:3.12-alpine

RUN apk add --no-cache openssl libstdc++ libgcc ca-certificates
COPY --from=telegram-api /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py start.py ./
RUN addgroup -S compiler && adduser -S -G compiler compiler \
    && mkdir -p /app/telegram-data /app/telegram-temp \
    && chown -R compiler:compiler /app
USER compiler
ENV PYTHONUNBUFFERED=1
CMD ["python", "-u", "start.py"]
