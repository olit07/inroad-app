#!/bin/bash
# Usage: bash deploy.sh staging | bash deploy.sh prod
ENV=${1:-}
if [ "$ENV" = "staging" ]; then
  echo "Deploying to STAGING..."
  railway up --environment staging --detach
elif [ "$ENV" = "prod" ]; then
  echo "Deploying to PRODUCTION..."
  railway up --environment production --detach
else
  echo "Usage: bash deploy.sh staging|prod"
  exit 1
fi
