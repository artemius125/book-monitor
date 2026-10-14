FROM mcr.microsoft.com/playwright/python:v1.64.0-noble
WORKDIR /app
COPY pyproject.toml requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY monitor ./monitor
RUN pip install --no-cache-dir --no-deps .
ENV MONITOR_CONTAINER=1 MONITOR_DATA_DIR=/app/data
EXPOSE 8765
CMD ["book-monitor", "serve"]
