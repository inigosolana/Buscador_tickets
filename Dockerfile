FROM mcr.microsoft.com/playwright/python:v1.54.0-jammy

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY autoscout_scraper.py /app/
COPY send_cars_to_telegram.py /app/
COPY telegram_bot_service.py /app/
COPY search_models.json /app/
COPY entrypoint.sh /app/
COPY bootstrap.sh /app/

RUN pip install --no-cache-dir requests beautifulsoup4 pgeocode playwright \
    && chmod +x /app/entrypoint.sh /app/bootstrap.sh

CMD ["/app/entrypoint.sh"]
