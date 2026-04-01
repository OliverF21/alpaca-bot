FROM python:3.13-slim

WORKDIR /app

# System deps for numpy/pandas compilation + curl for healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories the app expects
RUN mkdir -p logs equity_logs backtest_results /tmp/bot_logs

# Set Python to unbuffered output
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Health check endpoint (FastAPI server on port 8000)
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# Default: run all services (equity + crypto + web dashboard)
CMD ["python", "run_all.py"]
