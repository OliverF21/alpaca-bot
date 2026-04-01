# Docker Setup for Alpaca Bot

This guide explains how to run the Alpaca Bot using Docker.

## Prerequisites

- **Docker Desktop** installed and running
- **.env file** configured with Alpaca API credentials (see `.env.example`)

## Quick Start

### 1. Build and Run All Services
```bash
docker compose up -d --build
```

This starts:
- **Equity Scanner** — monitors stock positions
- **Crypto Scanner** — monitors crypto positions  
- **FastAPI Dashboard** — web UI on http://localhost:8000

### 2. View Logs
```bash
docker compose logs -f bot
```

### 3. Run Specific Services

**Equity + Web only (no crypto):**
```bash
docker compose run bot python run_all.py --no-crypto
```

**Crypto + Web only (weekend mode):**
```bash
docker compose run bot python run_all.py --no-equity
```

**Run equity scanner alone:**
```bash
docker compose run bot python scanner/run_scanner.py
```

**Run crypto scanner alone:**
```bash
docker compose run bot python scanner/run_crypto_scanner.py
```

### 4. Stop Services
```bash
docker compose down
```

## Configuration

### Environment Variables
The `.env` file is automatically loaded into the container. Required variables:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_PAPER` (true for paper trading, false for live)
- `POLYGON_API_KEY` (optional)

### Volumes
The docker-compose mounts these directories for persistence:
- `logs/` — scanner logs
- `equity_logs/` — equity history CSVs
- `backtest_results/` — backtesting outputs

### Port Mapping
- **8000** → FastAPI web dashboard

## Health Check

The container includes a health check that monitors the FastAPI server:
```bash
docker compose ps
```

Look for `(healthy)` status. The service is considered healthy after 40 seconds of startup and requires 3 successful health checks.

## Troubleshooting

### Container fails to start
1. Check logs: `docker compose logs bot`
2. Verify `.env` exists and has API credentials
3. Ensure Docker daemon is running

### Port 8000 already in use
```bash
docker compose down
# Or map to different port:
# Change `ports: - "8000:8000"` to `ports: - "8080:8000"` in docker-compose.yml
```

### Permission denied on volumes
```bash
docker compose down
sudo chown -R $(whoami):$(whoami) logs/ equity_logs/
docker compose up -d --build
```

## Development

### Rebuild after code changes
```bash
docker compose up -d --build
```

### Open a shell in the container
```bash
docker compose exec bot /bin/bash
```

### Run Python code directly
```bash
docker compose run bot python -c "import alpaca; print(alpaca.__version__)"
```

## Production Notes

- The container restarts automatically unless stopped with `docker compose down`
- Logs are persisted to `logs/` and `equity_logs/` directories
- Health checks ensure the web service is responsive
- Use `docker compose logs` to monitor activity
