FROM python:3.11-slim

LABEL maintainer="SentinelOps"
LABEL description="Fault-injectable sample app for SentinelOps incident generation"

WORKDIR /app

# Install dependencies first (layer cached separately from code)
RUN pip install --no-cache-dir fastapi uvicorn prometheus-client

# Create non-root user for security (avoid running as root in the container)
RUN useradd -r -u 1000 -g users appuser

# Copy application code
COPY app.py .

# Ensure the app user owns the application files
RUN chown -R appuser:users /app

USER appuser

# Expose application port
EXPOSE 8080

# Run with uvicorn; access logs help Promtail collect structured log data
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080", "--access-log"]
