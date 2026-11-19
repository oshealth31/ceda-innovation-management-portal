#!/usr/bin/env sh
# Installs and starts the CEDA Innovation Management Portal with Docker (Mac and Linux).
#   1. Install Docker Desktop (or Docker Engine with the compose plugin).
#   2. Run:  sh scripts/install.sh
#   3. Open http://localhost:8000 and sign in as "admin" with the password printed at the end.
# Run it again at any time to update: your data and settings are kept.
set -eu
cd "$(dirname "$0")/.."

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Get Docker Desktop from https://www.docker.com/products/docker-desktop/ and run this again."
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is missing. Update Docker Desktop, or install the docker-compose-plugin package, then run this again."
  exit 1
fi

random() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }

if [ ! -f .env ]; then
  ADMIN_PASSWORD="$(random 16)"
  {
    echo "# Created by scripts/install.sh on $(date). Keep this file private."
    echo "ENVIRONMENT=production"
    echo "JWT_SECRET=$(random 48)"
    echo "ADMIN_PASSWORD=$ADMIN_PASSWORD"
    echo "POSTGRES_PASSWORD=$(random 24)"
    echo "PORTAL_URL=http://localhost:8000"
    echo "# No email service is set up yet, so sign-up codes are shown on screen."
    echo "# Set BREVO_API_KEY and EMAIL_FROM (or SMTP_HOST), then change this to false."
    echo "DEMO_SHOW_CODES=true"
  } > .env
  chmod 600 .env
  echo "Created .env with new random passwords."
else
  ADMIN_PASSWORD="$(grep '^ADMIN_PASSWORD=' .env | cut -d= -f2-)"
  echo "Using the existing .env."
fi

echo "Building and starting the portal (the first time takes a few minutes)..."
docker compose up -d --build

printf "Waiting for the portal to be ready"
i=0
until curl -fsS http://localhost:8000/api/health/ready >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo
    echo "The portal did not start. See what went wrong with:  docker compose logs app"
    exit 1
  fi
  printf "."
  sleep 3
done
echo
echo
echo "The CEDA Innovation Management Portal is running at http://localhost:8000"
echo "Sign in as:  admin"
echo "Password:    $ADMIN_PASSWORD   (stored in .env; change it after signing in)"
echo
echo "Stop it:     docker compose down        (data is kept)"
echo "Update it:   git pull && sh scripts/install.sh"
