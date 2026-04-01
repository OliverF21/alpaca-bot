FROM python:3.13-slim

WORKDIR /app

# System deps for numpy/pandas compilation + curl for healthcheck + git + ssh
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ curl git ssh && \
    rm -rf /var/lib/apt/lists/*

# Set up SSH for git operations
RUN mkdir -p /root/.ssh && \
    ssh-keyscan -H github.com >> /root/.ssh/known_hosts 2>/dev/null

# Clone the latest code from GitHub using SSH
RUN --mount=type=ssh git clone git@github.com:OliverF21/alpaca-bot.git /app

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
