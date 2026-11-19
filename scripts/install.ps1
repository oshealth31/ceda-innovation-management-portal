# Installs and starts the CEDA Innovation Management Portal with Docker (Windows).
#   1. Install Docker Desktop and start it.
#   2. In PowerShell, from the project folder, run:  powershell -ExecutionPolicy Bypass -File scripts\install.ps1
#   3. Open http://localhost:8000 and sign in as "admin" with the password printed at the end.
# Run it again at any time to update: your data and settings are kept.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot '..')

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Write-Host 'Docker is not installed. Get Docker Desktop from https://www.docker.com/products/docker-desktop/ and run this again.'
  exit 1
}
docker compose version *> $null
if ($LASTEXITCODE -ne 0) {
  Write-Host 'Docker Compose is missing or Docker Desktop is not running. Start Docker Desktop and run this again.'
  exit 1
}

function New-RandomText([int]$length) {
  $chars = [char[]]'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
  $bytes = New-Object byte[] $length
  [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  -join ($bytes | ForEach-Object { $chars[$_ % $chars.Length] })
}

if (-not (Test-Path .env)) {
  $adminPassword = New-RandomText 16
  @(
    "# Created by scripts/install.ps1 on $(Get-Date). Keep this file private.",
    'ENVIRONMENT=production',
    "JWT_SECRET=$(New-RandomText 48)",
    "ADMIN_PASSWORD=$adminPassword",
    "POSTGRES_PASSWORD=$(New-RandomText 24)",
    'PORTAL_URL=http://localhost:8000',
    '# No email service is set up yet, so sign-up codes are shown on screen.',
    '# Set BREVO_API_KEY and EMAIL_FROM (or SMTP_HOST), then change this to false.',
    'DEMO_SHOW_CODES=true'
  ) | Set-Content -Encoding ascii .env
  Write-Host 'Created .env with new random passwords.'
} else {
  $adminPassword = ((Get-Content .env | Where-Object { $_ -like 'ADMIN_PASSWORD=*' }) -replace '^ADMIN_PASSWORD=', '')
  Write-Host 'Using the existing .env.'
}

Write-Host 'Building and starting the portal (the first time takes a few minutes)...'
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { Write-Host 'Docker could not start the portal. See:  docker compose logs app'; exit 1 }

Write-Host -NoNewline 'Waiting for the portal to be ready'
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  try {
    Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 http://localhost:8000/api/health/ready | Out-Null
    $ready = $true; break
  } catch { Write-Host -NoNewline '.'; Start-Sleep -Seconds 3 }
}
Write-Host ''
if (-not $ready) { Write-Host 'The portal did not start. See what went wrong with:  docker compose logs app'; exit 1 }

Write-Host ''
Write-Host 'The CEDA Innovation Management Portal is running at http://localhost:8000'
Write-Host 'Sign in as:  admin'
Write-Host "Password:    $adminPassword   (stored in .env; change it after signing in)"
Write-Host ''
Write-Host 'Stop it:     docker compose down        (data is kept)'
Write-Host 'Update it:   git pull, then run this script again'
