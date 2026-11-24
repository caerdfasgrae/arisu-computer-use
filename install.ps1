# One-Click Windows Installer for Arisu Computer Use (Antigravity Plugin)
param(
    [switch]$SkipTests
)

$ErrorActionPreference = "Stop"
$ProjectRoot = $PSScriptRoot

Write-Host "==========================================================" -ForegroundColor Cyan
Write-Host " [Arisu Computer Use] Windows 1-Click Installer" -ForegroundColor Cyan
Write-Host " Target User : $env:USERPROFILE" -ForegroundColor Cyan
Write-Host " Install Dir : $ProjectRoot" -ForegroundColor Cyan
Write-Host "==========================================================" -ForegroundColor Cyan

# 1. Check Python >= 3.11 (auto-install via winget if missing)
function Get-PythonCmd {
    foreach ($cmd in @("python", "py")) {
        if (Get-Command $cmd -ErrorAction SilentlyContinue) {
            try {
                $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($ver -match "^(\d+)\.(\d+)$") {
                    if ([int]$Matches[1] -gt 3 -or ([int]$Matches[1] -eq 3 -and [int]$Matches[2] -ge 11)) {
                        return $cmd
                    }
                }
            } catch {}
        }
    }
    $localPy = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "C:\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1
    if ($localPy) { return $localPy.FullName }
    return $null
}

$PyCmd = Get-PythonCmd
if (-not $PyCmd) {
    Write-Host "[1/3] Python 3.11+ belum terdeteksi. Menginstal Python 3.11 via winget..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Python.Python.3.11 --accept-package-agreements --accept-source-agreements --silent
        $env:PATH = "$env:LOCALAPPDATA\Programs\Python\Python311;$env:LOCALAPPDATA\Programs\Python\Python311\Scripts;C:\Python311;C:\Python311\Scripts;" + $env:PATH
        $PyCmd = Get-PythonCmd
    }
    if (-not $PyCmd) {
        Write-Error "Python 3.11+ tidak ditemukan. Install dari https://www.python.org/downloads/ lalu jalankan ulang install.ps1."
        exit 1
    }
}
Write-Host "[1/3] Python siap: $PyCmd" -ForegroundColor Green

# 2. Build .venv & register Antigravity plugin
Write-Host "[2/3] Menyiapkan environment Python (.venv) & memasang plugin Antigravity..." -ForegroundColor Cyan
Push-Location $ProjectRoot
try {
    if (Get-Command uv -ErrorAction SilentlyContinue) {
        uv sync
    } elseif (Test-Path "$HOME\.local\bin\uv.exe") {
        & "$HOME\.local\bin\uv.exe" sync
    } else {
        if (-not (Test-Path ".venv\Scripts\python.exe")) {
            & $PyCmd -m venv .venv
        }
        & ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
        & ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"
    }
    powershell -ExecutionPolicy Bypass -File ".\scripts\install_plugin.ps1"
} finally {
    Pop-Location
}

# 3. Verify with pytest
$VenvPy = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if ((-not $SkipTests) -and (Test-Path $VenvPy)) {
    Write-Host "[3/3] Menjalankan verifikasi pytest..." -ForegroundColor Cyan
    Push-Location $ProjectRoot
    try {
        & $VenvPy -m pytest -q
    } finally {
        Pop-Location
    }
}

Write-Host "==========================================================" -ForegroundColor Green
Write-Host " [SELESAI] Arisu Computer Use berhasil dipasang!" -ForegroundColor Green
Write-Host " Restart / reload jendela Antigravity 1x, lalu coba ketik:" -ForegroundColor Green
Write-Host "   /arisu-computer-use" -ForegroundColor Yellow
Write-Host "==========================================================" -ForegroundColor Green
