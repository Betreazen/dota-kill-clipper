# install.ps1 — подготовка dota-kill-clipper: папка клипов, GSI-конфиг Dota 2, подсказки для OBS.
# Запуск: powershell -ExecutionPolicy Bypass -File install.ps1 [-Root C:\Highlights] [-Port 3220]
param(
    [string]$Root = "C:\Highlights",
    [int]$Port = 3220
)
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$py312 = Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.12-64\python.exe"

Write-Host "== dota-kill-clipper =="
if (-not (Test-Path $py312)) {
    Write-Host "Python 3.12 не найден в $py312. Установите: py install 3.12  (Python install manager)" -ForegroundColor Yellow
    $py312 = "python"
}
if (-not (Test-Path $Root)) { New-Item -ItemType Directory -Path $Root | Out-Null }
Write-Host "Папка клипов: $Root"

$ffmpeg = Get-Command ffmpeg -ErrorAction SilentlyContinue
if (-not $ffmpeg -and -not (Test-Path "C:\ffmpeg\bin\ffmpeg.exe")) {
    Write-Host "ffmpeg не найден ни в PATH, ни в C:\ffmpeg\bin. Скачайте https://www.gyan.dev/ffmpeg/builds/ и распакуйте в C:\ffmpeg" -ForegroundColor Yellow
}

# GSI cfg + token (same file the OBS script reads: %LOCALAPPDATA%\dota-kill-clipper\token.txt)
$code = @"
import os, secrets, sys
sys.path.insert(0, r'$here')
from killclipper.dota_paths import install_cfg
d = os.path.join(os.environ['LOCALAPPDATA'], 'dota-kill-clipper'); os.makedirs(d, exist_ok=True)
t = os.path.join(d, 'token.txt')
token = open(t, encoding='utf-8').read().strip() if os.path.exists(t) else ''
if not token:
    token = secrets.token_hex(16); open(t, 'w', encoding='utf-8').write(token)
print('GSI cfg:', install_cfg($Port, token))
"@
& $py312 -c $code

Write-Host ""
Write-Host "Дальше в OBS (один раз):"
Write-Host "  1. Tools -> Scripts -> Python Settings -> путь: $env:LOCALAPPDATA\Python\pythoncore-3.12-64"
Write-Host "  2. Tools -> Scripts -> [+] -> $here\obs_dota_kill_clipper.py"
Write-Host "  3. Settings -> Output -> Recording: Replay Buffer включён, длина 120 с, память 2048 МБ (скрипт поднимет сам, но нужен перезапуск OBS)"
Write-Host "  4. Перезапустите Dota 2, если она была запущена (GSI-конфиг читается при старте)."
Write-Host "Лог: $env:LOCALAPPDATA\dota-kill-clipper\clipper.log"
