param(
    [string]$PgRoot = "C:\Program Files\PostgreSQL\18",
    [string]$PgVectorPath = "C:\Users\alison.maia\PoCs\pgvector"
)

$ErrorActionPreference = "Stop"

function Invoke-NMake([string]$Arguments, [string]$Step, [string]$LogPath) {
    $expr = "nmake $Arguments"
    Write-Host "Running: $expr"
    Invoke-Expression $expr | Tee-Object -FilePath $LogPath -Append
    if ($LASTEXITCODE -ne 0) {
        throw "nmake failed during '$Step' with exit code $LASTEXITCODE"
    }
}

function Import-VsDevEnvironment {
    $candidates = @(
        "${env:ProgramFiles}\Microsoft Visual Studio\18\BuildTools\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Community\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Professional\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles}\Microsoft Visual Studio\18\Enterprise\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\BuildTools\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Community\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Professional\Common7\Tools\VsDevCmd.bat",
        "${env:ProgramFiles(x86)}\Microsoft Visual Studio\2022\Enterprise\Common7\Tools\VsDevCmd.bat"
    )

    $vsDevCmd = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $vsDevCmd) {
        throw "VsDevCmd.bat not found. Install Visual Studio Build Tools 2022 with C++ workload and Windows SDK."
    }

    Write-Host "Using VS Dev Cmd: $vsDevCmd"
    $vsCmdLine = "`"$vsDevCmd`" -arch=x64 -host_arch=x64 && set"
    cmd /c $vsCmdLine | ForEach-Object {
        if ($_ -match "^(.*?)=(.*)$") {
            Set-Item -Path "Env:$($matches[1])" -Value $matches[2]
        }
    }
}

function Assert-Tool([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required tool not found in PATH: $Name"
    }
}

function Assert-Path([string]$PathToCheck, [string]$Message) {
    if (-not (Test-Path $PathToCheck)) {
        throw "$Message`nMissing path: $PathToCheck"
    }
}

Write-Host "Step 1: Importing Visual Studio build environment..."
Import-VsDevEnvironment

Write-Host "Step 2: Setting PostgreSQL environment..."
$env:PGROOT = $PgRoot
$env:PATH = "$PgRoot\bin;$env:PATH"

Write-Host "Step 3: Validating prerequisites..."
Assert-Tool "cl.exe"
Assert-Tool "nmake.exe"
Assert-Path "$PgRoot\include\server\postgres.h" "PostgreSQL server headers not found."
Assert-Path "$PgRoot\lib\postgres.lib" "PostgreSQL import library not found."
Assert-Path $PgVectorPath "pgvector source folder not found."

$crtMsVc = @()
if ($env:VCToolsInstallDir) {
    $crtMsVc = @(Get-ChildItem "$env:VCToolsInstallDir\include" -Filter "crtdefs.h" -Recurse -ErrorAction SilentlyContinue)
}
$crtKitX86 = @(Get-ChildItem "C:\Program Files (x86)\Windows Kits" -Filter "crtdefs.h" -Recurse -ErrorAction SilentlyContinue)
$crtKitX64 = @(Get-ChildItem "C:\Program Files\Windows Kits" -Filter "crtdefs.h" -Recurse -ErrorAction SilentlyContinue)
$crt = @($crtMsVc + $crtKitX86 + $crtKitX64) | Select-Object -First 1
if (-not $crt) {
    throw "crtdefs.h not found in MSVC or Windows Kits include paths. Repair Visual Studio C++ toolset and SDK."
}
Write-Host "Found crtdefs.h at: $($crt.FullName)"

Write-Host "Step 4: Building pgvector..."
Set-Location $PgVectorPath

$log = Join-Path $PgVectorPath "build.log"
if (Test-Path $log) {
    Remove-Item $log -Force
}

"=== pgvector build log ===" | Out-File -FilePath $log -Encoding utf8
Invoke-NMake -Arguments "/f Makefile.win clean" -Step "clean" -LogPath $log
Invoke-NMake -Arguments "/f Makefile.win" -Step "build" -LogPath $log
Invoke-NMake -Arguments "/f Makefile.win install" -Step "install" -LogPath $log

Write-Host ""
Write-Host "Build and install completed."
Write-Host "Log file: $log"
