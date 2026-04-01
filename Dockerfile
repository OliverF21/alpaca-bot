FROM python:3.13-slim

WORKDIR /app

# System deps for numpy/pandas compilation (if wheels unavailable)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create directories the app expects
RUN mkdir -p logs equity_logs /tmp/bot_logs

# Default: run all services (equity + crypto + web dashboard)
CMD ["python", "run_all.py"]
