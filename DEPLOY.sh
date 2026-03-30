#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Coffee Chat Connect — Railway deploy script
# Run from the ccc-backend directory: bash DEPLOY.sh
# ─────────────────────────────────────────────────────────────────────────────

set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

info()    { echo -e "${GREEN}[CCC]${NC} $1"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $1"; }
error()   { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# ── 1. Pre-flight checks ─────────────────────────────────────────────────────

info "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || error "python3 not found"
command -v git     >/dev/null 2>&1 || error "git not found"

if ! command -v railway >/dev/null 2>&1; then
  warn "Railway CLI not found. Installing..."
  if command -v npm >/dev/null 2>&1; then
    npm install -g @railway/cli
  else
    curl -fsSL https://railway.app/install.sh | sh
  fi
fi

# ── 2. Git init (if needed) ───────────────────────────────────────────────────

if [ ! -d .git ]; then
  info "Initialising git repo..."
  git init
  git add -A
  git commit -m "Initial commit — Coffee Chat Connect"
fi

# ── 3. Railway login ─────────────────────────────────────────────────────────

info "Logging into Railway (browser will open)..."
railway login

# ── 4. Railway project ───────────────────────────────────────────────────────

if [ ! -f .railway/config.json ] 2>/dev/null; then
  info "Creating Railway project..."
  railway init
fi

# ── 5. Set environment variables ─────────────────────────────────────────────

info "Setting environment variables..."

if [ ! -f .env ]; then
  warn ".env not found — copying from .env.example"
  cp .env.example .env
fi

# Load .env and push each var to Railway
set -a
source .env
set +a

railway variables set \
  APP_BASE_URL="https://$(railway domain 2>/dev/null || echo 'YOUR-APP.up.railway.app')" \
  SESSION_SECRET="${SESSION_SECRET}" \
  RESEND_API_KEY="${RESEND_API_KEY}" \
  SMTP_HOST="${SMTP_HOST}" \
  SMTP_PORT="${SMTP_PORT}" \
  SMTP_USER="${SMTP_USER}" \
  SMTP_PASS="${SMTP_PASS}" \
  FROM_EMAIL="${FROM_EMAIL}" \
  FROM_NAME="${FROM_NAME}" \
  SERPER_API_KEY="${SERPER_API_KEY}" \
  DEV_MODE="false" \
  2>/dev/null || warn "Some variables may need to be set manually in the Railway dashboard"

# ── 6. Add Postgres ──────────────────────────────────────────────────────────

info "Checking for Postgres plugin..."
railway add --plugin postgresql 2>/dev/null || warn "Postgres may already be attached — check dashboard"

# ── 7. Deploy ────────────────────────────────────────────────────────────────

info "Deploying to Railway..."
railway up --detach

# ── 8. Post-deploy ───────────────────────────────────────────────────────────

echo ""
info "Deploy triggered. Waiting for URL..."
sleep 5

RAILWAY_URL=$(railway domain 2>/dev/null || echo "")

if [ -n "$RAILWAY_URL" ]; then
  echo ""
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}  Deployed!${NC}"
  echo -e "${GREEN}  URL: https://${RAILWAY_URL}${NC}"
  echo -e "${GREEN}  Health: https://${RAILWAY_URL}/api/health${NC}"
  echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo ""
  warn "Next steps:"
  echo "  1. Update ALLOWED_ORIGINS in Railway dashboard to include https://${RAILWAY_URL}"
  echo "  2. Test the full magic link flow: https://${RAILWAY_URL}/signup"
  echo "  3. Add your custom domain in Railway → Settings → Domains"
  echo "  4. Verify your sending domain at resend.com and update FROM_EMAIL"
  echo ""
else
  warn "Could not detect Railway URL automatically."
  warn "Check https://railway.app/dashboard for your deployment URL."
fi
