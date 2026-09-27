# reset_lumi_history.ps1
# Generic history reset. Wipes chat history and confirms.

$ErrorActionPreference = "Stop"

$historyPath = Join-Path $PSScriptRoot "logs\chat_history.json"

if (Test-Path $historyPath) {
    Remove-Item $historyPath -Force
    Write-Host "History cleared." -ForegroundColor Green
} else {
    Write-Host "Nothing to clear." -ForegroundColor Yellow
}