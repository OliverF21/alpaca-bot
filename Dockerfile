FROM python:3.13-slim

WORKDIR /app

# System deps for numpy/pandas compilation + curl for healthcheck + git
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl git && \
    rm -rf /var/lib/apt/lists/*

# Clone the latest code from GitHub using token passed as build arg
ARG GITHUB_TOKEN
RUN git clone https://${GITHUB_TOKEN}@github.com/OliverF21/alpaca-bot.git /app && \
    git config --global --unset-all url.https.insteadOf  # Clean up git config after clone

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

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
