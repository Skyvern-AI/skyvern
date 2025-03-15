# PowerShell script for Windows entry point

param (
    [string]$DisplayNumber = "99",
    [string]$ScreenResolution = "1920x1080x16"
)

function Start-Xvfb {
    param (
        [string]$DisplayNumber,
        [string]$ScreenResolution
    )
    Write-Host "Starting Xvfb..."
    $xvfbProcess = Start-Process -FilePath "Xvfb" -ArgumentList ":$DisplayNumber -screen 0 $ScreenResolution" -PassThru
    return $xvfbProcess
}

function Stop-Xvfb {
    param (
        [System.Diagnostics.Process]$xvfbProcess
    )
    Write-Host "Stopping Xvfb..."
    if ($xvfbProcess -ne $null) {
        $xvfbProcess.Kill()
    }
}

function Run-Skyvern {
    Write-Host "Running Skyvern..."
    Start-Process -FilePath "python" -ArgumentList "-m skyvern.forge"
}

# Main script execution
try {
    $xvfbProcess = Start-Xvfb -DisplayNumber $DisplayNumber -ScreenResolution $ScreenResolution
    Start-Sleep -Seconds 5  # Give Xvfb some time to start

    # Set display environment variable
    $env:DISPLAY = ":$DisplayNumber"

    # Run Skyvern
    Run-Skyvern
} finally {
    Stop-Xvfb -xvfbProcess $xvfbProcess
}
