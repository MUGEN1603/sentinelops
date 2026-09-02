FROM python:3.11-slim

LABEL maintainer="SentinelOps"
LABEL description="Incident Normalizer — AlertManager webhook receiver and LangGraph pipeline dispatcher"

WORKDIR /app

# Install dependencies first (layer cached separately from code)
COPY requirements.txt .
RUN pip install --no-cache-dir \
    fastapi \
    uvicorn \
    pydantic \
    requests \
    langgraph \
    langgraph-checkpoint-sqlite \
    ollama \
    qdrant-client \
    fastembed \
    PyGithub

# Create non-root user for security
RUN useradd -r -u 1000 -g users appuser

# Copy application code — the normalizer needs agents/, memory/ for imports
COPY . /app/

# Ensure the app user owns the application files
RUN chown -R appuser:users /app

USER appuser

# Expose webhook port
EXPOSE 8000

# Health check for k8s liveness probe
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:8000/health').raise_for_status()" || exit 1

# Run the webhook server
CMD ["uvicorn", "webhook_server:app", "--host", "0.0.0.0", "--port", "8000", "--access-log"]
