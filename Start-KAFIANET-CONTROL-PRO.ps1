$ErrorActionPreference = "Stop"

# Python and the Qt runtime were moved to the local drive.
$pythonRoot = "D:\games\py"
$pythonExe = Join-Path -Path $pythonRoot -ChildPath "python.exe"
$libRoot = Join-Path -Path $pythonRoot -ChildPath "Lib"
$dllRoot = Join-Path -Path $pythonRoot -ChildPath "DLLs"
$appRoot = $PSScriptRoot
$sitePackages = Join-Path -Path $libRoot -ChildPath "site-packages"
$runtimeModule = Get-ChildItem -LiteralPath $appRoot -File -Filter "kafianet_external_runtime*.pyd" |
    Select-Object -First 1

function Stop-KafiaLauncher([int]$Code, [string]$Message) {
    $fullMessage = "[KAFIA NET CONTROL PRO] $Message"
    try {
        $shell = New-Object -ComObject WScript.Shell
        [void]$shell.Popup($fullMessage, 0, "KAFIA NET CONTROL PRO", 16)
    } catch {
        Write-Host $fullMessage
    }
    exit $Code
}

if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) {
    Stop-KafiaLauncher 10 "Python runtime was not found at D:\games\py. Confirm the local runtime folder is complete."
}
if (-not (Test-Path -LiteralPath $libRoot -PathType Container)) {
    Stop-KafiaLauncher 11 "The local Python Lib folder is missing."
}
if (-not (Test-Path -LiteralPath $dllRoot -PathType Container)) {
    Stop-KafiaLauncher 11 "The local Python DLLs folder is missing."
}
if (-not $runtimeModule) {
    Stop-KafiaLauncher 13 "The compiled application runtime module is missing."
}

$env:PYTHONHOME = $pythonRoot
$env:PYTHONUTF8 = "1"
$pythonPathItems = @($appRoot, $sitePackages, $env:PYTHONPATH) |
    Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$env:PYTHONPATH = $pythonPathItems -join ";"
$pathItems = @(
    $pythonRoot,
    $dllRoot,
    $sitePackages,
    (Join-Path $sitePackages "PySide6"),
    (Join-Path $sitePackages "PySide6\Qt\bin"),
    (Join-Path $sitePackages "shiboken6"),
    (Join-Path $appRoot "platform-tools"),
    $env:PATH
) | Where-Object { -not [string]::IsNullOrWhiteSpace($_) }
$env:PATH = $pathItems -join ";"

$bootstrapCommand = "import kafianet_external_runtime; raise SystemExit(kafianet_external_runtime.main())"

$hadQpaPlatform = Test-Path Env:QT_QPA_PLATFORM
$previousQpaPlatform = $env:QT_QPA_PLATFORM
$env:QT_QPA_PLATFORM = "offscreen"
& $pythonExe -c $bootstrapCommand --qt-import-smoke-test
$smokeExitCode = $LASTEXITCODE
if ($hadQpaPlatform) {
    $env:QT_QPA_PLATFORM = $previousQpaPlatform
} else {
    Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue
}
if ($smokeExitCode -ne 0) {
    Stop-KafiaLauncher 12 "Qt startup preflight failed. Check the Python 3.14 x64 runtime, PySide6 and Qt DLLs."
}

$previousLocation = Get-Location
try {
    Set-Location -LiteralPath $appRoot
    & $pythonExe -c $bootstrapCommand
    $appExitCode = $LASTEXITCODE
} catch {
    Stop-KafiaLauncher 14 ("Unable to launch the application: " + $_.Exception.Message)
} finally {
    Set-Location -LiteralPath $previousLocation.Path
}
if ($appExitCode -ne 0) {
    Stop-KafiaLauncher $appExitCode "The application exited with an error."
}
exit 0
