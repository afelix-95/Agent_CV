<#
.SYNOPSIS
    Build and push the agent-cv Docker image to Docker Hub (amaia95/agents).

.EXAMPLE
    # Uses version from pyproject.toml automatically
    .\deploy.ps1

    # Override version explicitly
    .\deploy.ps1 -Version 0.2.0
#>
param(
    [string]$Version = ""
)

$IMAGE = "amaia95/agents"
$ErrorActionPreference = "Stop"

# ── Resolve version ────────────────────────────────────────────────────────────
if (-not $Version) {
    $toml = Get-Content "$PSScriptRoot\pyproject.toml" -Raw
    if ($toml -match 'version\s*=\s*"([^"]+)"') {
        $Version = $Matches[1]
    } else {
        Write-Error "Could not determine version from pyproject.toml"
        exit 1
    }
}

Write-Host "Version  : $Version"
Write-Host "Image    : ${IMAGE}:${Version}"
Write-Host ""

# ── Build ──────────────────────────────────────────────────────────────────────
Write-Host "==> Building image..."
docker build `
    --build-arg APP_VERSION=$Version `
    -t "${IMAGE}:${Version}" `
    -t "${IMAGE}:latest" `
    $PSScriptRoot

if ($LASTEXITCODE -ne 0) { Write-Error "docker build failed"; exit 1 }

# ── Push ───────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "==> Pushing to Docker Hub..."
docker push "${IMAGE}:${Version}"
docker push "${IMAGE}:latest"

if ($LASTEXITCODE -ne 0) { Write-Error "docker push failed"; exit 1 }

# ── Done ───────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "Done. Image ${IMAGE}:${Version} is live on Docker Hub."
Write-Host ""
Write-Host "To update the remote server, SSH in and run:"
Write-Host "  docker compose pull app"
Write-Host "  docker compose up -d --no-deps app"
