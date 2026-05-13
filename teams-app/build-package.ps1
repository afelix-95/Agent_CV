# Build Teams app package (appPackage.zip) by substituting .env variables into the manifest.
# Usage: .\build-package.ps1

$envFile = Join-Path $PSScriptRoot ".env"
$appPackageDir = Join-Path $PSScriptRoot "appPackage"
$outZip = Join-Path $PSScriptRoot "appPackage.zip"

# Parse .env (skip comments and blank lines)
$env = @{}
Get-Content $envFile | Where-Object { $_ -match "^\s*[^#\s]" } | ForEach-Object {
    $parts = $_ -split "=", 2
    if ($parts.Length -eq 2) {
        $key = $parts[0].Trim()
        $value = ($parts[1] -split "#")[0].Trim()  # strip inline comments
        $env[$key] = $value
    }
}

# Substitute ${{VAR}} placeholders in manifest.json
$manifest = Get-Content (Join-Path $appPackageDir "manifest.json") -Raw
foreach ($key in $env.Keys) {
    $manifest = $manifest -replace [regex]::Escape("`${{$key}}"), $env[$key]
}

# Write substituted manifest to a temp location
$tempDir = Join-Path $PSScriptRoot ".build"
if (Test-Path $tempDir) { Remove-Item $tempDir -Recurse -Force }
New-Item -ItemType Directory -Path $tempDir | Out-Null
$manifest | Set-Content (Join-Path $tempDir "manifest.json") -Encoding UTF8

# Copy icon files
Copy-Item (Join-Path $appPackageDir "color.png") $tempDir
Copy-Item (Join-Path $appPackageDir "outline.png") $tempDir

# Zip
if (Test-Path $outZip) { Remove-Item $outZip -Force }
Compress-Archive -Path "$tempDir\*" -DestinationPath $outZip

# Clean up temp
Remove-Item $tempDir -Recurse -Force

Write-Host "Built: $outZip"
