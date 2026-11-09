FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --create-home app
COPY db_ui ./db_ui
COPY warehouse_sync ./warehouse_sync
COPY migrations ./migrations
USER app
ENV DB_UI_BIND=0.0.0.0 DB_UI_PORT=8765 PYTHONUNBUFFERED=1
EXPOSE 8765
CMD ["python", "-m", "db_ui"]
