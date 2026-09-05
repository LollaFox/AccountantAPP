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
Write-Host "If the iPhone is the hotspot, copy the https://172.20.10.x:$Port address from the pairing page."
try {
    $ruleName = "Receipt Sync iPhone 8765"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port -Profile Any -ErrorAction Stop | Out-Null
        Write-Host "Windows Firewall: inbound TCP $Port allowed."
    }
} catch {
    Write-Host "Windows Firewall rule was not added. If the iPhone cannot connect, allow inbound TCP $Port on the Public profile."
}
& $Python $serverScript --data-dir $DataDir --model-cache $ModelCache `
    --host 0.0.0.0 --port $Port --cert $certificatePath --key $keyPath `
    --review-port $ReviewPort
