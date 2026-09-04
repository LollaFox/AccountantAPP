param(
    [string]$Python = "C:\Users\15610\Documents\ChatGPT\DesktopAssistant\.venv-paddle\Scripts\python.exe",
    [string]$DataDir = "$env:LOCALAPPDATA\ReceiptSync",
    [string]$ModelCache = "$env:LOCALAPPDATA\ReceiptSync\paddle_models",
    [int]$Port = 8765,
    [int]$ReviewPort = 8764
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "PaddleOCR Python was not found: $Python"
}

$certificateDirectory = Join-Path $DataDir "tls"
$certificatePath = Join-Path $certificateDirectory "receipt-sync-cert.pem"
$keyPath = Join-Path $certificateDirectory "receipt-sync-key.pem"

if (-not (Test-Path -LiteralPath $certificatePath) -or -not (Test-Path -LiteralPath $keyPath)) {
    & "$PSScriptRoot\generate_certificate.ps1" -OutputDirectory $certificateDirectory
}

$serverScript = "$PSScriptRoot\receipt_sync_server.py"

Write-Host "Computer review: http://127.0.0.1:$ReviewPort"
Write-Host "iPhone HTTPS port: $Port"
& $Python $serverScript --data-dir $DataDir --model-cache $ModelCache `
    --host 0.0.0.0 --port $Port --cert $certificatePath --key $keyPath `
    --review-port $ReviewPort
