param(
    [switch]$Fast,
    [switch]$Clean,
    [switch]$Ci,
    [switch]$Diagnose
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$AppName = 'Windows-PC-Locker'
$AppVersion = '2.3 SAFE'
$NumericVersion = '2.3.0.0'
$EntryPoint = 'computer_locker.pyw'
$PythonSeries = '3.13'
$FallbackPythonVersion = '3.13.15'
$FallbackPythonInstallerUrl = 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe'
$FallbackPythonInstallerSha256 = 'edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403'
$BuildVenv = Join-Path $ProjectRoot '.build-venv'
$BuildPython = Join-Path $BuildVenv 'Scripts\python.exe'
$BuildRoot = Join-Path $ProjectRoot '.build'
$BuildCache = Join-Path $ProjectRoot '.build-cache'
$BuildLogs = Join-Path $ProjectRoot 'build_logs'
$CurrentDist = Join-Path $ProjectRoot 'dist'
$PreviousDist = Join-Path $ProjectRoot 'dist_previous'
$BuildRequirements = Join-Path $ProjectRoot 'requirements-build.txt'
$Timestamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$LogPath = Join-Path $BuildLogs "build_$Timestamp.log"
$SummaryPath = Join-Path $BuildLogs 'last_build_summary.json'
$Stage = 'bootstrap'
$FailureMessage = $null
$PythonInfo = $null
$PyInstallerVersion = $null
$GitCommit = 'source-archive'
$TranscriptStarted = $false

function Write-Step([string]$Message) {
    Write-Host "[$Stage] $Message"
}

function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @(),
        [Parameter(Mandatory = $true)][string]$Label
    )
    Write-Host "[RUN] $Label"
    & $FilePath @Arguments
    $code = $LASTEXITCODE
    if ($code -ne 0) {
        throw "$Label failed with exit code $code"
    }
}

function Invoke-TimedExecutable {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(Mandatory = $false)][string[]]$Arguments = @(),
        [int]$TimeoutSeconds = 60,
        [string]$WorkingDirectory = $null
    )
    $start = @{
        FilePath = $FilePath
        ArgumentList = $Arguments
        PassThru = $true
    }
    if ($WorkingDirectory) {
        $start.WorkingDirectory = $WorkingDirectory
    }
    $process = Start-Process @start
    if (-not $process.WaitForExit($TimeoutSeconds * 1000)) {
        try { $process.Kill() } catch { }
        throw "Timed out after $TimeoutSeconds seconds: $FilePath $($Arguments -join ' ')"
    }
    if ($process.ExitCode -ne 0) {
        throw "Process failed with exit code $($process.ExitCode): $FilePath $($Arguments -join ' ')"
    }
}

function Remove-PathSafe([string]$Path) {
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}

function Test-PythonCandidate {
    param(
        [Parameter(Mandatory = $true)][string]$Command,
        [string[]]$PrefixArguments = @()
    )
    try {
        $code = "import json,platform,sys; print(json.dumps({'exe':sys.executable,'series':f'{sys.version_info[0]}.{sys.version_info[1]}','full':platform.python_version(),'arch':platform.architecture()[0]}))"
        $raw = & $Command @PrefixArguments -c $code 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $raw) { return $null }
        $info = ($raw | Select-Object -Last 1) | ConvertFrom-Json
        if ($info.series -ne $PythonSeries -or $info.arch -ne '64bit') { return $null }
        return [pscustomobject]@{
            executable = [string]$info.exe
            version = [string]$info.full
            series = [string]$info.series
            arch = [string]$info.arch
        }
    } catch {
        return $null
    }
}

function Find-Python {
    if ($env:pythonLocation) {
        $candidate = Join-Path $env:pythonLocation 'python.exe'
        if (Test-Path $candidate) {
            $result = Test-PythonCandidate -Command $candidate
            if ($result) { return $result }
        }
    }

    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        try {
            $exe = & py.exe -3.13 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $result = Test-PythonCandidate -Command ($exe | Select-Object -Last 1)
                if ($result) { return $result }
            }
        } catch { }
    }

    $known = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:ProgramFiles 'Python313\python.exe')
    )
    foreach ($candidate in $known) {
        if (Test-Path $candidate) {
            $result = Test-PythonCandidate -Command $candidate
            if ($result) { return $result }
        }
    }

    $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) {
        $result = Test-PythonCandidate -Command $pythonCommand.Source
        if ($result) { return $result }
    }
    return $null
}

function Invoke-DownloadWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Uri,
        [Parameter(Mandatory = $true)][string]$Destination,
        [int]$Attempts = 3
    )
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        try {
            Write-Host "[DOWNLOAD] $Uri (attempt $attempt/$Attempts)"
            Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $Destination -TimeoutSec 120
            return
        } catch {
            if ($attempt -eq $Attempts) { throw }
            Start-Sleep -Seconds ([Math]::Min(8, 2 * $attempt))
        }
    }
}

function Install-PythonIfNeeded {
    if ($Ci) {
        throw "Python $PythonSeries x64 is missing in CI. setup-python must provide it; CI will not modify the runner globally."
    }

    $winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Step "Python $PythonSeries was not found. Trying per-user installation with winget..."
        try {
            & $winget.Source install --id Python.Python.3.13 -e --scope user --silent --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -eq 0) {
                $found = Find-Python
                if ($found) { return $found }
            }
        } catch {
            Write-Warning "winget Python installation failed; using verified python.org fallback."
        }
    }

    Write-Step "Using verified python.org fallback installer $FallbackPythonVersion..."
    New-Item -ItemType Directory -Force -Path $BuildCache | Out-Null
    $installer = Join-Path $BuildCache "python-$FallbackPythonVersion-amd64.exe"
    if (-not (Test-Path $installer)) {
        Invoke-DownloadWithRetry -Uri $FallbackPythonInstallerUrl -Destination $installer
    }
    $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
    if ($actualHash -ne $FallbackPythonInstallerSha256) {
        Remove-Item -LiteralPath $installer -Force -ErrorAction SilentlyContinue
        throw "Python installer SHA-256 mismatch. Expected $FallbackPythonInstallerSha256, got $actualHash"
    }

    $installArgs = @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1',
        'Include_pip=1', 'Include_test=0', 'SimpleInstall=1'
    )
    $proc = Start-Process -FilePath $installer -ArgumentList $installArgs -PassThru -Wait
    if ($proc.ExitCode -ne 0) {
        throw "Official Python installer failed with exit code $($proc.ExitCode)"
    }
    $found = Find-Python
    if (-not $found) {
        throw "Python installation completed but Python $PythonSeries x64 still cannot be located."
    }
    return $found
}

function Get-GitCommit {
    $git = Get-Command git.exe -ErrorAction SilentlyContinue
    if (-not $git) { return 'source-archive' }
    try {
        $value = & $git.Source -C $ProjectRoot rev-parse HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $value) { return ($value | Select-Object -Last 1).Trim() }
    } catch { }
    return 'source-archive'
}

function Write-BuildSummary {
    param([string]$Status, [string]$ErrorText = $null, [string]$Artifact = $null)
    New-Item -ItemType Directory -Force -Path $BuildLogs | Out-Null
    $summary = [ordered]@{
        schema = 1
        app = $AppName
        app_version = $AppVersion
        status = $Status
        stage = $Stage
        error = $ErrorText
        artifact = $Artifact
        build_log = $LogPath
        generated_at_utc = [DateTime]::UtcNow.ToString('o')
        git_commit = $GitCommit
        python = if ($PythonInfo) { $PythonInfo.version } else { $null }
        python_executable = if ($PythonInfo) { $PythonInfo.executable } else { $null }
        pyinstaller = $PyInstallerVersion
        fast = [bool]$Fast
        clean = [bool]$Clean
        ci = [bool]$Ci
        diagnose = [bool]$Diagnose
    }
    $summary | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $SummaryPath -Encoding UTF8
}

function Assert-Preflight {
    $script:Stage = 'preflight'
    Write-Step 'Checking Windows, architecture, disk space and write access...'
    if ($env:OS -ne 'Windows_NT') { throw 'This build pipeline supports Windows only.' }
    if ($env:PROCESSOR_ARCHITECTURE -notin @('AMD64', 'ARM64')) {
        throw "Unsupported Windows architecture: $env:PROCESSOR_ARCHITECTURE"
    }
    if (-not (Test-Path (Join-Path $ProjectRoot $EntryPoint))) { throw "Missing entry point: $EntryPoint" }
    if (-not (Test-Path $BuildRequirements)) { throw 'Missing requirements-build.txt' }
    if ($ProjectRoot.Length -gt 180) {
        Write-Warning "Project path is long ($($ProjectRoot.Length) chars). Move it closer to the drive root if third-party tools fail."
    }
    $probe = Join-Path $ProjectRoot '.build_write_probe.tmp'
    try {
        'ok' | Set-Content -LiteralPath $probe -Encoding ASCII
    } finally {
        Remove-Item -LiteralPath $probe -Force -ErrorAction SilentlyContinue
    }
    $driveName = ([IO.Path]::GetPathRoot($ProjectRoot)).Substring(0, 1)
    $drive = Get-PSDrive -Name $driveName
    $freeGb = [Math]::Round($drive.Free / 1GB, 2)
    Write-Host "[INFO] Free space: $freeGb GB"
    if ($drive.Free -lt 2GB) { throw 'At least 2 GB of free disk space is required for a safe build.' }
}

function Prepare-Toolchain {
    $script:Stage = 'toolchain'
    if ($Clean) {
        Write-Step 'Clean mode: removing build venv and caches (current dist is preserved until verification succeeds)...'
        Remove-PathSafe $BuildVenv
        Remove-PathSafe $BuildCache
        Remove-PathSafe $BuildRoot
    }

    $script:PythonInfo = Find-Python
    if (-not $PythonInfo) {
        if ($Diagnose) { throw "Python $PythonSeries x64 is not installed. A normal build would install it automatically." }
        $script:PythonInfo = Install-PythonIfNeeded
    }
    Write-Host "[INFO] Python: $($PythonInfo.version) x64 at $($PythonInfo.executable)"

    if ($Diagnose) {
        if (Test-Path $BuildPython) {
            Write-Host "[INFO] Existing build venv: $BuildPython"
        } else {
            Write-Host '[INFO] Build venv does not exist yet; normal build will create it.'
        }
        return
    }

    if (Test-Path $BuildPython) {
        try {
            $venvInfo = Test-PythonCandidate -Command $BuildPython
            if (-not $venvInfo) {
                Write-Step 'Existing build venv is incompatible; recreating it.'
                Remove-PathSafe $BuildVenv
            }
        } catch {
            Remove-PathSafe $BuildVenv
        }
    }
    if (-not (Test-Path $BuildPython)) {
        Write-Step 'Creating isolated build environment...'
        Invoke-Native -FilePath $PythonInfo.executable -Arguments @('-m', 'venv', $BuildVenv) -Label 'Create build venv'
    }

    $toolchainMarker = Join-Path $BuildVenv '.toolchain-id'
    $expectedMarker = (Get-Content -LiteralPath $BuildRequirements -Raw).Trim()
    $currentMarker = if (Test-Path $toolchainMarker) { (Get-Content -LiteralPath $toolchainMarker -Raw).Trim() } else { '' }
    if (-not $Fast -or $currentMarker -ne $expectedMarker) {
        Write-Step 'Installing pinned build toolchain...'
        Invoke-Native -FilePath $BuildPython -Arguments @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '--timeout', '60', '--retries', '2', '-r', $BuildRequirements) -Label 'Install build requirements'
        $expectedMarker | Set-Content -LiteralPath $toolchainMarker -Encoding UTF8
    } else {
        Write-Step 'Fast mode: pinned build toolchain is already present.'
    }
    Invoke-Native -FilePath $BuildPython -Arguments @('-m', 'pip', 'check') -Label 'pip check'
    $script:PyInstallerVersion = (& $BuildPython -m PyInstaller --version | Select-Object -Last 1).Trim()
    Write-Host "[INFO] PyInstaller: $PyInstallerVersion"
}

function Generate-WindowsMetadata {
    $script:Stage = 'metadata'
    $metaDir = Join-Path $BuildRoot 'metadata'
    New-Item -ItemType Directory -Force -Path $metaDir | Out-Null
    $manifest = Join-Path $metaDir 'app.manifest'
    $versionFile = Join-Path $metaDir 'version_info.txt'

    @"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
  <assemblyIdentity version="$NumericVersion" processorArchitecture="amd64" name="Windows.PC.Locker" type="win32"/>
  <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3">
    <security><requestedPrivileges><requestedExecutionLevel level="asInvoker" uiAccess="false"/></requestedPrivileges></security>
  </trustInfo>
  <application xmlns="urn:schemas-microsoft-com:asm.v3">
    <windowsSettings>
      <dpiAwareness xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">PerMonitorV2,PerMonitor</dpiAwareness>
      <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
    </windowsSettings>
  </application>
</assembly>
"@ | Set-Content -LiteralPath $manifest -Encoding UTF8

    @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=(2,3,0,0), prodvers=(2,3,0,0), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Dmitry Kolesnichenko'),
      StringStruct('FileDescription', 'Windows PC Locker'),
      StringStruct('FileVersion', '2.3.0.0'),
      StringStruct('InternalName', 'Windows-PC-Locker'),
      StringStruct('OriginalFilename', 'Windows-PC-Locker.exe'),
      StringStruct('ProductName', 'Windows PC Locker'),
      StringStruct('ProductVersion', '2.3 SAFE')
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"@ | Set-Content -LiteralPath $versionFile -Encoding UTF8
    return [pscustomobject]@{ manifest = $manifest; versionFile = $versionFile }
}

function Run-SourceVerification {
    $script:Stage = 'source-verification'
    Write-Step 'Compiling source and running the safe source self-test...'
    Invoke-Native -FilePath $BuildPython -Arguments @('-m', 'py_compile', $EntryPoint) -Label 'Compile source'
    Invoke-Native -FilePath $BuildPython -Arguments @($EntryPoint, '--self-test') -Label 'Source self-test'
}

function Build-Package {
    param($Metadata)
    $script:Stage = 'pyinstaller'
    $stageDist = Join-Path $BuildRoot 'stage-dist'
    $workPath = Join-Path $BuildRoot 'pyinstaller-work'
    $specPath = Join-Path $BuildRoot 'spec'
    Remove-PathSafe $stageDist
    if (-not $Fast) { Remove-PathSafe $workPath }
    Remove-PathSafe $specPath
    New-Item -ItemType Directory -Force -Path $stageDist, $workPath, $specPath | Out-Null

    $args = @('-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed', '--noupx', '--name', $AppName,
        '--distpath', $stageDist, '--workpath', $workPath, '--specpath', $specPath,
        '--manifest', $Metadata.manifest, '--version-file', $Metadata.versionFile)
    if (-not $Fast) { $args += '--clean' }
    $args += $EntryPoint
    Invoke-Native -FilePath $BuildPython -Arguments $args -Label 'PyInstaller package'

    $exe = Join-Path $stageDist "$AppName.exe"
    if (-not (Test-Path $exe)) { throw "Expected EXE was not created: $exe" }
    $sizeMb = [Math]::Round((Get-Item $exe).Length / 1MB, 2)
    Write-Host "[INFO] EXE size: $sizeMb MB"
    if ((Get-Item $exe).Length -lt 2MB) { throw "EXE is unexpectedly small ($sizeMb MB); refusing to publish a likely incomplete package." }
    return $exe
}

function Verify-PackagedExe {
    param([string]$ExePath)
    $script:Stage = 'packaged-self-test'
    Write-Step 'Running packaged self-test from staging...'
    Invoke-TimedExecutable -FilePath $ExePath -Arguments @('--self-test') -TimeoutSeconds 60 -WorkingDirectory (Split-Path $ExePath -Parent)

    $script:Stage = 'portable-folder-test'
    $portableRoot = Join-Path $env:TEMP ("Проверка сборки Windows PC Locker " + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $portableRoot | Out-Null
    try {
        $portableExe = Join-Path $portableRoot "$AppName.exe"
        Copy-Item -LiteralPath $ExePath -Destination $portableExe -Force
        Write-Step 'Running EXE from an isolated path containing spaces and Cyrillic...'
        Invoke-TimedExecutable -FilePath $portableExe -Arguments @('--self-test') -TimeoutSeconds 60 -WorkingDirectory $portableRoot
    } finally {
        Remove-PathSafe $portableRoot
    }
}

function Create-VerifiedDistribution {
    param([string]$ExePath)
    $script:Stage = 'distribution'
    $packageDir = Join-Path $BuildRoot 'package'
    $finalStage = Join-Path $BuildRoot 'final-dist'
    Remove-PathSafe $packageDir
    Remove-PathSafe $finalStage
    New-Item -ItemType Directory -Force -Path $packageDir, $finalStage | Out-Null

    $packagedExe = Join-Path $packageDir "$AppName.exe"
    Copy-Item -LiteralPath $ExePath -Destination $packagedExe -Force
    $exeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $packagedExe).Hash.ToLowerInvariant()
    $script:GitCommit = Get-GitCommit

    $buildInfo = [ordered]@{
        schema = 1
        app = $AppName
        app_version = $AppVersion
        artifact_type = 'onefile-portable-x64'
        built_at_utc = [DateTime]::UtcNow.ToString('o')
        git_commit = $GitCommit
        python = $PythonInfo.version
        pyinstaller = $PyInstallerVersion
        architecture = 'x64'
        source_entrypoint = $EntryPoint
        exe = "$AppName.exe"
        exe_sha256 = $exeHash
        verification = @('source-compile', 'source-self-test', 'packaged-self-test', 'portable-folder-test', 'zip-extract-self-test')
    }
    $buildInfo | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $packageDir 'build_info.json') -Encoding UTF8

    $zipName = "$AppName-portable-x64.zip"
    $zipPath = Join-Path $BuildRoot $zipName
    Remove-Item -LiteralPath $zipPath -Force -ErrorAction SilentlyContinue
    Compress-Archive -Path (Join-Path $packageDir '*') -DestinationPath $zipPath -CompressionLevel Optimal

    $script:Stage = 'zip-verification'
    $extractRoot = Join-Path $env:TEMP ("Проверка ZIP Windows PC Locker " + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Force -Path $extractRoot | Out-Null
    try {
        Expand-Archive -LiteralPath $zipPath -DestinationPath $extractRoot -Force
        $zipExe = Join-Path $extractRoot "$AppName.exe"
        if (-not (Test-Path $zipExe)) { throw 'ZIP verification failed: EXE is missing after extraction.' }
        $zipExeHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipExe).Hash.ToLowerInvariant()
        if ($zipExeHash -ne $exeHash) { throw 'ZIP verification failed: extracted EXE hash does not match.' }
        Invoke-TimedExecutable -FilePath $zipExe -Arguments @('--self-test') -TimeoutSeconds 60 -WorkingDirectory $extractRoot
    } finally {
        Remove-PathSafe $extractRoot
    }

    $zipHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $zipPath).Hash.ToLowerInvariant()
    Copy-Item -LiteralPath $packagedExe -Destination (Join-Path $finalStage "$AppName.exe") -Force
    Copy-Item -LiteralPath (Join-Path $packageDir 'build_info.json') -Destination (Join-Path $finalStage 'build_info.json') -Force
    Copy-Item -LiteralPath $zipPath -Destination (Join-Path $finalStage $zipName) -Force
    @(
        "$exeHash *$AppName.exe",
        "$zipHash *$zipName"
    ) | Set-Content -LiteralPath (Join-Path $finalStage 'SHA256SUMS.txt') -Encoding ASCII

    $script:Stage = 'atomic-publish'
    Write-Step 'Publishing only after every verification stage succeeded...'
    Remove-PathSafe $PreviousDist
    if (Test-Path $CurrentDist) {
        Move-Item -LiteralPath $CurrentDist -Destination $PreviousDist
    }
    try {
        Move-Item -LiteralPath $finalStage -Destination $CurrentDist
    } catch {
        if ((-not (Test-Path $CurrentDist)) -and (Test-Path $PreviousDist)) {
            Move-Item -LiteralPath $PreviousDist -Destination $CurrentDist
        }
        throw
    }
    return (Join-Path $CurrentDist "$AppName.exe")
}

$exitCode = 0
try {
    New-Item -ItemType Directory -Force -Path $BuildLogs | Out-Null
    Get-ChildItem -LiteralPath $BuildLogs -Filter 'build_*.log' -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 20 | Remove-Item -Force -ErrorAction SilentlyContinue
    try {
        Start-Transcript -LiteralPath $LogPath -Force | Out-Null
        $TranscriptStarted = $true
    } catch {
        Write-Warning "Could not start PowerShell transcript: $($_.Exception.Message)"
    }

    $GitCommit = Get-GitCommit
    Assert-Preflight
    Prepare-Toolchain
    if ($Diagnose) {
        $Stage = 'diagnose-complete'
        Write-Host '[OK] Diagnostic preflight completed. No build or dependency installation was performed.'
        Write-BuildSummary -Status 'diagnose-ok'
    } else {
        New-Item -ItemType Directory -Force -Path $BuildRoot | Out-Null
        $metadata = Generate-WindowsMetadata
        Run-SourceVerification
        $exe = Build-Package -Metadata $metadata
        Verify-PackagedExe -ExePath $exe
        $artifact = Create-VerifiedDistribution -ExePath $exe
        $Stage = 'complete'
        Write-BuildSummary -Status 'success' -Artifact $artifact
        Write-Host ''
        Write-Host '[OK] READY BUILD CREATED AND VERIFIED'
        Write-Host "EXE: $artifact"
        Write-Host "ZIP: $(Join-Path $CurrentDist "$AppName-portable-x64.zip")"
        Write-Host "SHA256: $(Join-Path $CurrentDist 'SHA256SUMS.txt')"
        Write-Host "Build info: $(Join-Path $CurrentDist 'build_info.json')"
        if (Test-Path $PreviousDist) {
            Write-Host "Previous good distribution: $PreviousDist"
        }
    }
} catch {
    $exitCode = 1
    $FailureMessage = $_.Exception.Message
    Write-Host ''
    Write-Host "[ERROR] Build failed at stage '$Stage': $FailureMessage" -ForegroundColor Red
    try { Write-BuildSummary -Status 'failed' -ErrorText $FailureMessage } catch { }
    Write-Host "[INFO] Current dist was not replaced unless the complete candidate had already passed every verification stage."
    Write-Host "[INFO] Summary: $SummaryPath"
} finally {
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
}

exit $exitCode
