param(
    [int]$ProxyPort = 9223,
    [string]$EnvPath = ".env",
    [switch]$UpdateEnv,
    [switch]$NoFirewall
)

$ErrorActionPreference = "Stop"

function Find-DevToolsActivePort {
    $roots = @()
    if ($env:LOCALAPPDATA) {
        $roots += Join-Path $env:LOCALAPPDATA "Google\Chrome\User Data"
    }
    if ($env:TEMP) {
        $roots += $env:TEMP
    }

    $files = foreach ($root in $roots) {
        if (Test-Path $root) {
            Get-ChildItem $root -Recurse -Filter DevToolsActivePort -ErrorAction SilentlyContinue
        }
    }

    $files | Sort-Object LastWriteTime -Descending | Select-Object -First 1
}

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )

    $line = "$Name=$Value"
    if (Test-Path $Path) {
        $contents = @(Get-Content $Path)
        $updated = $false
        $contents = $contents | ForEach-Object {
            if ($_ -match "^\s*$([regex]::Escape($Name))\s*=") {
                $updated = $true
                $line
            } else {
                $_
            }
        }
        if (-not $updated) {
            $contents += $line
        }
        $contents | Set-Content -Encoding ascii $Path
    } else {
        $line | Set-Content -Encoding ascii $Path
    }
}

$portFile = Find-DevToolsActivePort
if (-not $portFile) {
    Write-Error "DevToolsActivePort was not found. Open Chrome, go to chrome://inspect/#remote-debugging, enable remote debugging, then rerun this script."
}

$lines = Get-Content $portFile.FullName
if ($lines.Count -lt 2) {
    Write-Error "DevToolsActivePort at $($portFile.FullName) did not contain a port and browser path."
}

$chromePort = [int]$lines[0]
$browserPath = [string]$lines[1]
if (-not $browserPath.StartsWith("/devtools/browser/")) {
    Write-Error "DevToolsActivePort path '$browserPath' is not a browser websocket endpoint."
}

Write-Host "Found Chrome inspect endpoint:" -ForegroundColor Cyan
Write-Host "  File: $($portFile.FullName)"
Write-Host "  Chrome port: $chromePort"
Write-Host "  Browser path: $browserPath"

& netsh interface portproxy delete v4tov4 listenaddress=0.0.0.0 listenport=$ProxyPort *> $null
& netsh interface portproxy add v4tov4 listenaddress=0.0.0.0 listenport=$ProxyPort connectaddress=127.0.0.1 connectport=$chromePort
if ($LASTEXITCODE -ne 0) {
    Write-Error "Failed to create Windows portproxy. Rerun PowerShell as Administrator."
}

if (-not $NoFirewall) {
    $ruleName = "Skyvern Chrome Inspect Proxy $ProxyPort"
    $existingRule = Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue
    if (-not $existingRule) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -LocalPort $ProxyPort -Protocol TCP -Action Allow | Out-Null
    }
}

$remoteUrl = "ws://host.docker.internal:$ProxyPort$browserPath"

if ($UpdateEnv) {
    Set-EnvValue -Path $EnvPath -Name "BROWSER_TYPE" -Value "cdp-connect"
    Set-EnvValue -Path $EnvPath -Name "BROWSER_REMOTE_DEBUGGING_URL" -Value $remoteUrl
    Set-EnvValue -Path $EnvPath -Name "BROWSER_STREAMING_MODE" -Value "cdp"
    Set-EnvValue -Path $EnvPath -Name "BROWSER_CDP_CONNECT_TIMEOUT_MS" -Value "120000"
    Write-Host "Updated $EnvPath for Skyvern Docker Compose." -ForegroundColor Green
}

Write-Host ""
Write-Host "Use this Skyvern setting:" -ForegroundColor Green
Write-Host "BROWSER_REMOTE_DEBUGGING_URL=$remoteUrl"
Write-Host ""
Write-Host "Restart the backend after changing .env:"
Write-Host "docker compose up -d --force-recreate skyvern"
Write-Host ""
Write-Host "Chrome may prompt to allow remote debugging on first connection. Click Allow, then retry if the first attempt times out."
