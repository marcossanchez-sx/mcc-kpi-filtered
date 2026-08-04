# Sube el proyecto a GitHub desde PowerShell.
#
#   .\push.ps1
#
# El repositorio remoto se creó con un README de inicialización, así que la primera
# subida tiene que reconciliar ese commit. Se hace con --force-with-lease, que
# sobrescribe sólo si nadie ha tocado `main` desde el último fetch: si alguien lo ha
# modificado, el push falla en lugar de pisar su trabajo.

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

$remote = 'https://github.com/marcossanchez-sx/mcc-kpi-filtered.git'

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Error "git no está en el PATH. Instálalo desde https://git-scm.com/download/win"
}

if (-not (Test-Path .git)) {
    Write-Host "No hay repositorio git aquí. Inicializando..." -ForegroundColor Yellow
    git init
    git branch -M main
    git add -A
    git commit -m "MCC KPI Filtered: backend del detection rate"
}

# Configura el remoto (idempotente)
$existing = git remote get-url origin 2>$null
if ($LASTEXITCODE -ne 0) { git remote add origin $remote } else { git remote set-url origin $remote }

Write-Host "`nComprobando que no se suba ningún dato operativo..." -ForegroundColor Cyan
$leaks = git ls-files | Where-Object { $_ -like '*.csv' -and $_ -notlike '*example*' }
if ($leaks) {
    Write-Host "ABORTADO: estos CSV con datos reales están en el commit:" -ForegroundColor Red
    $leaks | ForEach-Object { Write-Host "  $_" -ForegroundColor Red }
    Write-Error "Revisa .gitignore antes de subir."
}
Write-Host "  correcto: sólo plantillas .example.csv" -ForegroundColor Green

Write-Host "`nDescargando el estado del remoto..." -ForegroundColor Cyan
git fetch origin

Write-Host "`nSubiendo..." -ForegroundColor Cyan
git push -u --force-with-lease origin main

Write-Host "`nListo: https://github.com/marcossanchez-sx/mcc-kpi-filtered" -ForegroundColor Green
