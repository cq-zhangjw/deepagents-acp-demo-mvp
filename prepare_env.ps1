$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

# ==================== Config ====================
$VenvDir = '.venv'
$Requirements = 'requirements.txt'
$TargetVersionPrefix = '3.12.'
$InstallDir = 'C:\Program Files\Python\python3120'
$PortablePythonVersion = '3.12.8'
$PortableDownloadUrl = "https://www.nuget.org/api/v2/package/python/$PortablePythonVersion"
$PortablePackagePath = Join-Path $env:TEMP "python.$PortablePythonVersion.nupkg"
$PortableExtractTempDir = Join-Path $env:TEMP "python-$PortablePythonVersion-portable"
$InstalledPythonExe = Join-Path $InstallDir 'tools\python.exe'
# ===============================================

function Pause-AndExit {
    param(
        [int]$Code = 0,
        [string]$Message = 'Press Enter to exit'
    )

    Write-Host $Message
    Read-Host | Out-Null
    exit $Code
}

function Get-PythonVersionString {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PythonExe
    )

    try {
        $output = & $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($output)) {
            return $null
        }
        return $output.Trim()
    } catch {
        return $null
    }
}

function Test-Is312 {
    param(
        [string]$Version
    )

    if ([string]::IsNullOrWhiteSpace($Version)) {
        return $false
    }
    return $Version.StartsWith($TargetVersionPrefix)
}

function Get-VenvPythonPath {
    param(
        [string]$VenvPath
    )

    return Join-Path $VenvPath 'Scripts\python.exe'
}

function Find-Python312 {
    # 1) Try python from PATH
    $pythonCmd = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $pythonCmd -and -not [string]::IsNullOrWhiteSpace($pythonCmd.Source)) {
        $v = Get-PythonVersionString -PythonExe $pythonCmd.Source
        if (Test-Is312 -Version $v) {
            return @{ Path = $pythonCmd.Source; Version = $v; Source = 'python PATH' }
        }
    }

    # 2) Try py launcher for 3.12 (if installed)
    $pyCmd = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $pyCmd) {
        try {
            $py312Exe = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null).Trim()
            if (-not [string]::IsNullOrWhiteSpace($py312Exe) -and (Test-Path -Path $py312Exe -PathType Leaf)) {
                $v = Get-PythonVersionString -PythonExe $py312Exe
                if (Test-Is312 -Version $v) {
                    return @{ Path = $py312Exe; Version = $v; Source = 'py -3.12 launcher' }
                }
            }
        } catch {
            # Ignore and continue searching
        }
    }

    # 3) Try common installation locations
    $candidates = @(
        (Join-Path $env:LocalAppData 'Programs\Python\Python312\python.exe'),
        'C:\Program Files\Python312\python.exe',
        (Join-Path $InstallDir 'python.exe'),
        (Join-Path $InstallDir 'tools\python.exe')
    )

    foreach ($candidate in $candidates) {
        if (Test-Path -Path $candidate -PathType Leaf) {
            $v = Get-PythonVersionString -PythonExe $candidate
            if (Test-Is312 -Version $v) {
                return @{ Path = $candidate; Version = $v; Source = 'common install path' }
            }
        }
    }

    return $null
}

function Test-IsAdmin {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    return $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Invoke-SelfElevate {
    param(
        [string]$ScriptPath
    )

    Write-Host '[INFO] Administrator privileges are required. A UAC confirmation prompt will be shown.'

    $escapedScriptPath = '"' + $ScriptPath + '"'
    $argumentList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $escapedScriptPath, '-Elevated')

    try {
        $proc = Start-Process -FilePath 'powershell.exe' -ArgumentList $argumentList -Verb RunAs -Wait -PassThru
        return $proc.ExitCode
    } catch {
        Write-Host 'Error: Elevation request was canceled or failed.'
        return 1
    }
}

function Install-PortablePython {
    param(
        [string]$DestinationDir,
        [string]$DownloadUrl,
        [string]$PackagePath,
        [string]$TempExtractDir,
        [string]$ExpectedPythonExe
    )

    if (-not (Test-IsAdmin)) {
        throw 'Administrator privileges are required to write into Program Files.'
    }

    Write-Host "[INFO] Downloading portable Python package from: $DownloadUrl"
    Invoke-WebRequest -Uri $DownloadUrl -OutFile $PackagePath

    if (Test-Path -Path $TempExtractDir -PathType Container) {
        Remove-Item -Path $TempExtractDir -Recurse -Force
    }
    New-Item -Path $TempExtractDir -ItemType Directory -Force | Out-Null

    Write-Host '[INFO] Extracting portable package...'
    Expand-Archive -Path $PackagePath -DestinationPath $TempExtractDir -Force

    if (Test-Path -Path $DestinationDir -PathType Container) {
        Remove-Item -Path $DestinationDir -Recurse -Force
    }
    New-Item -Path $DestinationDir -ItemType Directory -Force | Out-Null

    Write-Host "[INFO] Installing portable Python into $DestinationDir"
    Copy-Item -Path (Join-Path $TempExtractDir '*') -Destination $DestinationDir -Recurse -Force

    Remove-Item -Path $PackagePath -Force -ErrorAction SilentlyContinue
    Remove-Item -Path $TempExtractDir -Recurse -Force -ErrorAction SilentlyContinue

    if (-not (Test-Path -Path $ExpectedPythonExe -PathType Leaf)) {
        throw "Portable Python install failed, executable not found: $ExpectedPythonExe"
    }
}

function Ensure-PipAvailable {
    param(
        [string]$PythonExe
    )

    & $PythonExe -m pip --version 2>$null
    if ($LASTEXITCODE -eq 0) {
        return
    }

    Write-Host '[INFO] pip not found, trying ensurepip...'
    & $PythonExe -m ensurepip --upgrade
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to bootstrap pip with ensurepip.'
    }

    & $PythonExe -m pip --version 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw 'pip is still unavailable after ensurepip.'
    }
}

Write-Host '[INFO] Initializing environment checks...'

$isElevatedRun = $false
if ($args -contains '-Elevated') {
    $isElevatedRun = $true
}

# 1) Check current .venv exists and is Python 3.12.x
if (Test-Path -Path $VenvDir -PathType Container) {
    $venvPython = Get-VenvPythonPath -VenvPath $VenvDir
    if (Test-Path -Path $venvPython -PathType Leaf) {
        $venvVersion = Get-PythonVersionString -PythonExe $venvPython
        if (Test-Is312 -Version $venvVersion) {
            Write-Host "[OK] Virtual environment $VenvDir detected with version $venvVersion, which satisfies the 3.12.x requirement."
            Pause-AndExit -Code 0 -Message 'Environment check completed. Press Enter to exit'
        } else {
            Write-Host "[WARN] Virtual environment $VenvDir detected, but version is $venvVersion (not 3.12.x). Environment will be prepared again."
        }
    } else {
        Write-Host "[WARN] Directory $VenvDir exists, but no usable python.exe was found. Environment will be prepared again."
    }
} else {
    Write-Host "[INFO] Virtual environment directory $VenvDir was not found."
}

# 2) Find a usable Python 3.12.x first; install only when none is found
$selectedPython = $null
$foundPython = Find-Python312
if ($null -ne $foundPython) {
    Write-Host "[INFO] Python 3.12.x found via $($foundPython.Source): $($foundPython.Path) ($($foundPython.Version))"
    $selectedPython = $foundPython.Path
} else {
    Write-Host '[INFO] No usable Python 3.12.x detected. Installation will be attempted.'
}

if (-not $selectedPython) {
    Write-Host "[INFO] Preparing to install portable Python 3.12.x to $InstallDir"

    # Needs admin to install to Program Files
    if (-not (Test-IsAdmin)) {
        if (-not $isElevatedRun) {
            $childExitCode = Invoke-SelfElevate -ScriptPath $PSCommandPath
            if ($childExitCode -eq 0) {
                Write-Host '[INFO] Elevated window completed successfully. Closing this window.'
                exit 0
            }
            Write-Host '[WARN] Elevated setup did not complete successfully. Closing this window.'
            exit $childExitCode
        }

        Write-Host 'Error: This elevated run still does not have Administrator privileges.'
        Pause-AndExit -Code 1
    }

    try {
        Install-PortablePython -DestinationDir $InstallDir -DownloadUrl $PortableDownloadUrl -PackagePath $PortablePackagePath -TempExtractDir $PortableExtractTempDir -ExpectedPythonExe $InstalledPythonExe
    } catch {
        Write-Host "Error: Portable Python installation failed. $($_.Exception.Message)"
        Pause-AndExit -Code 1
    }

    $installedVersion = Get-PythonVersionString -PythonExe $InstalledPythonExe
    if (-not (Test-Is312 -Version $installedVersion)) {
        Write-Host "Error: Installed portable Python version is unexpected ($installedVersion) and does not satisfy 3.12.x."
        Pause-AndExit -Code 1
    }

    Write-Host "[INFO] Portable Python installation succeeded: $InstalledPythonExe ($installedVersion)"
    $selectedPython = $InstalledPythonExe
}

# 3) Create virtual environment with selected Python
if (Test-Path -Path $VenvDir -PathType Container) {
    Write-Host "[INFO] Existing $VenvDir found. Removing and recreating it."
    Remove-Item -Path $VenvDir -Recurse -Force
}

Write-Host "[INFO] Creating virtual environment using $selectedPython..."
& $selectedPython -m venv --copies $VenvDir
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Error: Failed to create virtual environment. Please check Python venv module.'
    Pause-AndExit -Code 1
}

# 4) Activate venv and install requirements.txt
$activateScript = Join-Path $VenvDir 'Scripts\Activate.ps1'
if (-not (Test-Path -Path $activateScript -PathType Leaf)) {
    Write-Host 'Error: Virtual environment activation script was not found. Creation may have failed.'
    Pause-AndExit -Code 1
}

Write-Host '[INFO] Activating virtual environment...'
& $activateScript

if (-not (Test-Path -Path $Requirements -PathType Leaf)) {
    Write-Host "Error: Dependency file $Requirements was not found."
    Pause-AndExit -Code 1
}

Write-Host '[INFO] Ensuring pip is available in the selected Python...'
try {
    Ensure-PipAvailable -PythonExe $selectedPython
} catch {
    Write-Host "Error: $($_.Exception.Message)"
    Pause-AndExit -Code 1
}

Write-Host '[INFO] Upgrading pip...'
python -m pip install --upgrade pip -i https://pypi.tuna.tsinghua.edu.cn/simple
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Error: Failed to upgrade pip.'
    Pause-AndExit -Code 1
}

Write-Host '[INFO] Installing requirements.txt...'
pip install -r $Requirements -i https://pypi.tuna.tsinghua.edu.cn/simple
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Error: Dependency installation failed. Please check requirements.txt.'
    Pause-AndExit -Code 1
}

# 5) Done
Write-Host ''
Write-Host '[OK] Environment setup is complete.'
Write-Host '[OK] You can now use run.ps1 to start the application.'
Pause-AndExit -Code 0 -Message 'Press Enter to exit'
