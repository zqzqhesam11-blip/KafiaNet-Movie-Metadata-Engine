param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
$root = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonVersion = & python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion.Trim() -ne "3.14") {
    throw "Protected native modules must be compiled with 64-bit Python 3.14."
}
$pythonArch = & python -c "import struct; print(struct.calcsize('P') * 8)"
if ($LASTEXITCODE -ne 0 -or $pythonArch.Trim() -ne "64") {
    throw "Protected build requires 64-bit Python."
}
python -c "from importlib.metadata import version; import PySide6; print('Nuitka:', version('Nuitka')); print('PySide6:', PySide6.__version__)"
if ($LASTEXITCODE -ne 0) { throw "Nuitka or PySide6 is unavailable." }

$out = [System.IO.Path]::GetFullPath((Join-Path $root $OutputDirectory))
$rootPrefix = $root.TrimEnd('\') + '\'
if (-not $out.StartsWith($rootPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Output directory must be inside the repository build folder."
}
if (Test-Path -LiteralPath $out) {
    $existing = @(Get-ChildItem -LiteralPath $out -Force)
    if ($existing.Count -gt 0) {
        throw "Output directory is not empty; refusing to overwrite existing files: $out"
    }
} else {
    New-Item -ItemType Directory -Path $out -Force | Out-Null
}
$out = (Resolve-Path -LiteralPath $out).Path

$launcherText = Get-Content (Join-Path $root "Start-KAFIANET-CONTROL-PRO.ps1") -Raw
$bootstrapText = Get-Content (Join-Path $root "kafianet_external_runtime.py") -Raw
if (-not $launcherText.Contains('D:\games\py') -or $launcherText.Contains('SERVER06\c$')) {
    throw "PowerShell launcher must use the local runtime, D:\games\py."
}
if (-not $bootstrapText.Contains('Path(r"D:\games\py")')) {
    throw "Runtime bootstrap does not use the local D:\games\py runtime."
}

$tempRoot = Join-Path $env:RUNNER_TEMP ("kafianet-protected-" + [Guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tempRoot -Force | Out-Null
$qrcPath = Join-Path $tempRoot "kafianet_resources.qrc"
$resourcePy = Join-Path $tempRoot "ui_resources_rc.py"
$previousQpaPlatform = $env:QT_QPA_PLATFORM
$hadQpaPlatform = Test-Path Env:QT_QPA_PLATFORM
$previousSmokeFlag = $env:KAFIA_NET_CI_RUNTIME_SMOKE
$hadSmokeFlag = Test-Path Env:KAFIA_NET_CI_RUNTIME_SMOKE

try {
    $resourceFiles = @()
    $resourceFiles += Get-Item (Join-Path $root "games.json")
    $resourceFiles += Get-ChildItem (Join-Path $root "qml") -Recurse -File -Filter "*.qml" |
        Where-Object { $_.Name -ne "main_login_test.qml" }
    $assetsPath = Join-Path $root "assets"
    if (Test-Path -LiteralPath $assetsPath -PathType Container) {
        $resourceFiles += Get-ChildItem $assetsPath -Recurse -File
    }
    $activeQml = [System.IO.Path]::GetFullPath((Join-Path $root "qml\main.qml"))
    if (-not ($resourceFiles | Where-Object { $_.FullName -eq $activeQml })) {
        throw "The active qml/main.qml file was not included in the Qt resource bundle."
    }

    $qrcLines = [System.Collections.Generic.List[string]]::new()
    $qrcLines.Add('<RCC>')
    $qrcLines.Add('  <qresource prefix="/">')
    foreach ($file in $resourceFiles) {
        $relative = $file.FullName.Substring($root.Length).TrimStart('\', '/').Replace('\', '/')
        $safePath = [System.Security.SecurityElement]::Escape($file.FullName.Replace('\', '/'))
        $qrcLines.Add(('    <file alias="{0}">{1}</file>' -f $relative, $safePath))
    }
    $qrcLines.Add('  </qresource>')
    $qrcLines.Add('</RCC>')
    Set-Content -LiteralPath $qrcPath -Value $qrcLines -Encoding UTF8

    $rccCommand = (Get-Command "pyside6-rcc" -ErrorAction Stop).Source
    & $rccCommand $qrcPath -o $resourcePy
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $resourcePy -PathType Leaf)) {
        throw "pyside6-rcc failed to generate the embedded Qt resources."
    }

    $engineOut = Join-Path $out "engine"
    $uiOut = Join-Path $out "ui"
    New-Item -ItemType Directory -Path $engineOut -Force | Out-Null
    New-Item -ItemType Directory -Path $uiOut -Force | Out-Null

    $compileItems = [System.Collections.Generic.List[object]]::new()
    $compileItems.Add([pscustomobject]@{ Source = (Join-Path $root "app_qml.py"); Target = $out })
    $compileItems.Add([pscustomobject]@{ Source = (Join-Path $root "kafianet_external_runtime.py"); Target = $out })
    $compileItems.Add([pscustomobject]@{ Source = $resourcePy; Target = $out })

    foreach ($folderName in @("engine", "ui")) {
        $sourceFolder = Join-Path $root $folderName
        $targetFolder = if ($folderName -eq "engine") { $engineOut } else { $uiOut }
        foreach ($sourceFile in (Get-ChildItem -LiteralPath $sourceFolder -File -Filter "*.py" | Sort-Object Name)) {
            if ($sourceFile.Length -eq 0) { continue }
            $compileItems.Add([pscustomobject]@{ Source = $sourceFile.FullName; Target = $targetFolder })
        }
    }

    foreach ($item in $compileItems) {
        Write-Host ("Compiling native extension: {0}" -f [System.IO.Path]::GetFileName($item.Source))
        & python -m nuitka --module --msvc=latest --remove-output "--output-dir=$($item.Target)" "$($item.Source)"
        if ($LASTEXITCODE -ne 0) {
            throw "Nuitka failed while compiling $($item.Source)."
        }
    }

    foreach ($name in @(
        "Start-KAFIANET-CONTROL-PRO.cmd",
        "Start-KAFIANET-CONTROL-PRO.ps1",
        "Launch-KAFIA-NET-CONTROL-PRO.vbs"
    )) {
        Copy-Item -LiteralPath (Join-Path $root $name) -Destination $out
    }

    $platformToolsOut = Join-Path $out "platform-tools"
    New-Item -ItemType Directory -Path $platformToolsOut -Force | Out-Null
    foreach ($name in @("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll")) {
        $source = Join-Path (Join-Path $root "platform-tools") $name
        if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
            throw "Required ADB file is missing: $source"
        }
        Copy-Item -LiteralPath $source -Destination $platformToolsOut
    }

    Get-ChildItem -LiteralPath $out -Recurse -File -Filter "*.pyi" | Remove-Item -Force
    $nativeModules = @(Get-ChildItem -LiteralPath $out -Recurse -File -Filter "*.pyd")
    foreach ($name in @("app_qml", "kafianet_external_runtime", "ui_resources_rc")) {
        if (-not ($nativeModules | Where-Object { $_.BaseName -like "$name*" })) {
            throw "Expected compiled native module is missing: $name"
        }
    }
    foreach ($folderName in @("engine", "ui")) {
        $folderNative = @(Get-ChildItem -LiteralPath (Join-Path $out $folderName) -Recurse -File -Filter "*.pyd")
        if (-not $folderNative) { throw "No native application modules were compiled into $folderName." }
    }

    $rawPython = @(Get-ChildItem -LiteralPath $out -Recurse -File -Filter "*.py")
    $rawQml = @(Get-ChildItem -LiteralPath $out -Recurse -File -Filter "*.qml")
    $rawJson = @(Get-ChildItem -LiteralPath $out -Recurse -File -Filter "games.json")
    $pythonRuntimeBinaries = @(Get-ChildItem -LiteralPath $out -Recurse -File | Where-Object {
        $_.Name -match '^(python(?:w|3|3\d+)?\.exe|python(?:3|3\d+)?\.dll|vc_redist.*\.exe)$'
    })
    if ($rawPython -or $rawQml -or $rawJson -or $pythonRuntimeBinaries) {
        throw "Source/runtime leakage detected in protected output; refusing to publish."
    }

    $env:KAFIA_NET_CI_RUNTIME_SMOKE = "1"
    $env:QT_QPA_PLATFORM = "offscreen"
    Push-Location $out
    try {
        $bootstrapCommand = "import kafianet_external_runtime; raise SystemExit(kafianet_external_runtime.main())"
        Write-Host "Smoke-test: compiled Qt/Python runtime import"
        & python -c $bootstrapCommand --qt-import-smoke-test
        if ($LASTEXITCODE -ne 0) { throw "Compiled Qt import smoke test failed." }
        Write-Host "Smoke-test: embedded QML, assets, game config and compiled application"
        & python -c $bootstrapCommand --protected-build-smoke-test
        if ($LASTEXITCODE -ne 0) { throw "Protected application/QML smoke test failed." }
    } finally {
        Pop-Location
        if ($hadQpaPlatform) { $env:QT_QPA_PLATFORM = $previousQpaPlatform } else { Remove-Item Env:QT_QPA_PLATFORM -ErrorAction SilentlyContinue }
        if ($hadSmokeFlag) { $env:KAFIA_NET_CI_RUNTIME_SMOKE = $previousSmokeFlag } else { Remove-Item Env:KAFIA_NET_CI_RUNTIME_SMOKE -ErrorAction SilentlyContinue }
    }

    Write-Host "PROTECTED_PAYLOAD_VALIDATION: PASS"
    Write-Host ("Compiled native modules: {0}" -f $nativeModules.Count)
    Write-Host ("Protected payload size: {0:N2} MB" -f ((Get-ChildItem -LiteralPath $out -Recurse -File | Measure-Object -Property Length -Sum).Sum / 1MB))
} finally {
    if (Test-Path -LiteralPath $qrcPath) { Remove-Item -LiteralPath $qrcPath -Force }
    if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Recurse -Force }
}
