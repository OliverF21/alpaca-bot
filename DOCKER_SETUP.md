# Docker Setup with Private Repository

This Docker setup uses GitHub Personal Access Tokens (PAT) to securely clone from the private repository.

## Setup Instructions

### 1. Create a GitHub Personal Access Token

1. Go to https://github.com/settings/tokens
2. Click "Generate new token" → "Tokens (classic)"
3. Give it a name like "alpaca-bot-docker"
4. Select the **`repo`** scope (full control of private repositories)
5. Click "Generate token" and **copy the token immediately** (you won't see it again)

### 2. Create `.env.docker` File

```bash
cp .env.docker.example .env.docker
```

Edit `.env.docker` and paste your token:

```
GITHUB_TOKEN=ghp_your_token_here
```

**⚠️ IMPORTANT:** Never commit `.env.docker` to git. It's already in `.gitignore`.

### 3. Build and Run Docker

```bash
# Load the token and build
docker compose build bot

# Run the scanner
docker compose run bot python scanner/run_scanner.py

# Or start all services in background
docker compose up -d --build
```

### 4. Verify It Works

```bash
# Check container is running
docker compose ps

# View logs
docker compose logs -f bot
```

## How It Works

1. Docker reads `GITHUB_TOKEN` from `.env.docker`
2. The Dockerfile uses the token to clone: `https://${GITHUB_TOKEN}@github.com/OliverF21/alpaca-bot.git`
3. Git config is cleaned after clone to remove credential traces
4. All subsequent runs use the cloned code inside the container

## Token Security

- ✅ Token stored only in `.env.docker` (ignored by git)
- ✅ Token passed only during build time (not stored in image)
- ✅ Git config cleaned after clone
- ⚠️ If you rotate the token, rebuild: `docker compose build --no-cache bot`

## Troubleshooting

**"authentication failed" error:**
- Verify token is correct and has `repo` scope
- Token might be expired (regenerate a new one)

**"could not read Username" error:**
- Make sure `.env.docker` exists and `GITHUB_TOKEN` is set

**Need to change token:**
```bash
# Edit .env.docker with new token
nano .env.docker

# Rebuild without cache
docker compose build --no-cache bot
```
