$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$SourcePlugin = Join-Path $Root "plugin"
$GlobalPlugins = Join-Path $env:USERPROFILE ".gemini\config\plugins"
$TargetPlugin = Join-Path $GlobalPlugins "arisu-computer-use"
$SpikePlugin = Join-Path $GlobalPlugins "arisu-computer-use-spike"
$PyPath = Join-Path $Root ".venv\Scripts\python.exe"

if (-not (Test-Path $PyPath)) {
    Write-Error "Virtualenv Python tidak ditemukan: $PyPath. Jalankan 'uv sync' terlebih dulu."
    exit 1
}

if (Test-Path $TargetPlugin) {
    $NestedPlugin = Join-Path $TargetPlugin "plugin"
    if (Test-Path $NestedPlugin) {
        Remove-Item -Path $NestedPlugin -Recurse -Force -ErrorAction SilentlyContinue
    }
} else {
    New-Item -ItemType Directory -Path $TargetPlugin -Force | Out-Null
}

Copy-Item -Path (Join-Path $SourcePlugin "*") -Destination $TargetPlugin -Recurse -Force

$TargetMcp = Join-Path $TargetPlugin "mcp_config.json"
if (Test-Path $TargetMcp) {
    $mcpJson = Get-Content -Path $TargetMcp -Raw -Encoding UTF8 | ConvertFrom-Json
    $mcpJson.mcpServers."arisu-cua".command = $PyPath
    if ($null -eq $mcpJson.mcpServers."arisu-cua".env) {
        $mcpJson.mcpServers."arisu-cua" | Add-Member -NotePropertyName "env" -NotePropertyValue ([PSCustomObject]@{}) -Force
    }
    $buildTs = [DateTimeOffset]::Now.ToUnixTimeSeconds().ToString()
    $mcpJson.mcpServers."arisu-cua".env | Add-Member -NotePropertyName "ARISU_BUILD_TS" -NotePropertyValue $buildTs -Force
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($TargetMcp, ($mcpJson | ConvertTo-Json -Depth 6), $utf8NoBom)
}

Write-Host "Plugin Arisu Computer Use terpasang di: $TargetPlugin" -ForegroundColor Green

if (Test-Path $SpikePlugin) {
    try {
        Remove-Item -Path $SpikePlugin -Recurse -Force -ErrorAction Stop
        Write-Host "Plugin spike sementara dihapus: $SpikePlugin" -ForegroundColor Yellow
    } catch {
        Write-Warning "Folder plugin spike masih dipakai Antigravity ($SpikePlugin). Nonaktifkan via Settings atau hapus setelah restart."
    }
}
