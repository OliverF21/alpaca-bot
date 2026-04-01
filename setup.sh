#!/usr/bin/env bash
# Alpaca Bot — one-command setup for any machine
# Usage:
#   chmod +x setup.sh && ./setup.sh
#
# After setup, configure your API keys:
#   nano .env
# Then run:
#   source .venv/bin/activate
#   python run_all.py

set -e

echo "=== Alpaca Bot Setup ==="

# Check Python version
PYTHON=""
for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        major=$("$cmd" -c "import sys; print(sys.version_info.major)")
        minor=$("$cmd" -c "import sys; print(sys.version_info.minor)")
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python 3.11+ is required. Found: $version"
    exit 1
fi
echo "Using $PYTHON ($version)"

# Create virtual environment
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    $PYTHON -m venv .venv
fi

# Activate and install dependencies
echo "Installing dependencies..."
source .venv/bin/activate
pip install --upgrade pip -q
pip install -r requirements.txt -q

# Create .env from template if it doesn't exist
if [ ! -f ".env" ]; then
    cp .env.example .env
    echo ""
    echo "Created .env from template."
    echo ">>> IMPORTANT: Edit .env with your Alpaca API keys before running <<<"
    echo "    nano .env"
    echo ""
fi

# Create required directories
mkdir -p logs equity_logs /tmp/bot_logs

echo ""
echo "=== Setup complete ==="
echo ""
echo "Next steps:"
echo "  1. Edit .env with your Alpaca API keys (if not done already)"
echo "  2. Activate the environment:  source .venv/bin/activate"
echo "  3. Run the bot:               python run_all.py"
echo ""
echo "Other commands:"
echo "  python run_all.py --no-crypto      # equity scanner + web only"
echo "  python scanner/run_scanner.py      # equity scanner only"
echo "  python webapp/server.py            # web dashboard only (port 8000)"
echo "  python -m pytest strategy_ide/tests/ -v   # run tests"
