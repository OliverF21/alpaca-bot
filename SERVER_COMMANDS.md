# Server Commands

## Docker (recommended)

### Build & Start
```bash
docker compose up -d --build        # build image + start in background
docker compose up -d                 # start (skip build if image exists)
```

### Override Services
```bash
docker compose run bot python run_all.py --no-crypto    # equity + web only
docker compose run bot python run_all.py --no-equity    # crypto + web (weekend)
docker compose run bot python run_all.py --no-web       # scanners only
```

### Restart (picks up code changes)
```bash
docker compose down && docker compose up -d --build
```

### Logs
```bash
docker compose logs -f               # all services
docker compose logs -f bot            # just the bot container
docker exec alpaca-bot tail -f logs/crypto_scanner_$(date +%Y%m%d).log
docker exec alpaca-bot tail -f logs/equity_scanner_$(date +%Y%m%d).log
docker exec alpaca-bot tail -20 logs/orchestrator.log
```

### Status
```bash
docker compose ps                    # container status
docker exec alpaca-bot ps aux | grep python   # processes inside container
```

### Stop
```bash
docker compose down                  # stop and remove container
```

---

## Local (without Docker)

### Start
```bash
cd "/Users/oliver/VSCode Repos/alpaca-bot"
python run_all.py                    # equity + crypto + web dashboard
python run_all.py --no-crypto        # equity + web only
python run_all.py --no-equity        # crypto + web (weekend mode)
python run_all.py --no-web           # scanners only
```

### Individual Services
```bash
python scanner/run_scanner.py        # equity scanner (15-min, NYSE hours)
python scanner/run_crypto_scanner.py # crypto scanner (1h, 24/7, 4 strategies)
python webapp/server.py              # web dashboard on :8000
```

### Restart
```bash
pkill -f "run_all.py"; pkill -f "run_scanner.py"; pkill -f "run_crypto_scanner.py"; pkill -f "webapp/server.py"
sleep 2
cd "/Users/oliver/VSCode Repos/alpaca-bot" && python run_all.py
```

### Logs
```bash
tail -f logs/crypto_scanner_$(date +%Y%m%d).log
tail -f logs/equity_scanner_$(date +%Y%m%d).log
tail -20 logs/orchestrator.log
```

### Kill Everything
```bash
pkill -f "run_all.py"; pkill -f "run_scanner.py"; pkill -f "run_crypto_scanner.py"; pkill -f "webapp/server.py"
```

### Check for Zombies
```bash
ps aux | grep python | grep -E "run_all|run_scanner|run_crypto|server.py" | grep -v grep
```

---

## Dashboard
http://localhost:8000
