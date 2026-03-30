#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Coffee Chat Connect — local dev start script
# Run from the ccc-backend directory: bash START.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

GREEN='\033[0;32m'
NC='\033[0m'

if [ ! -f .env ]; then
  echo "No .env found — copying from .env.example"
  cp .env.example .env
fi

echo -e "${GREEN}[CCC]${NC} Installing dependencies..."
pip3 install -r requirements.txt -q

echo -e "${GREEN}[CCC]${NC} Loading environment..."
set -a
source .env
set +a

echo -e "${GREEN}[CCC]${NC} Starting server on http://localhost:${PORT:-5001}"
echo -e "${GREEN}[CCC]${NC} DEV_MODE=true — magic links printed to console, no emails sent"
echo ""

python3 api/server.py
