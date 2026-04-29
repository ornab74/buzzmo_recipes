Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RepoUrl = if ($env:BUZZMOO_REPO_URL) { $env:BUZZMOO_REPO_URL } else { "https://github.com/ornab74/buzzmoo_cooks.git" }
$AppDir = if ($env:BUZZMOO_COOKS_DIR) { $env:BUZZMOO_COOKS_DIR } else { Join-Path $HOME "buzzmoo_cooks" }

function Say($Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Cyan
}

function Warn($Message) {
    Write-Host ""
    Write-Host $Message -ForegroundColor Yellow
}

function Test-Command($Name) {
    $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Install-WingetPackage($Id) {
    if (-not (Test-Command winget)) {
        throw "winget is required. Install App Installer from Microsoft Store, then rerun this script."
    }
    $existing = winget list --id $Id -e 2>$null
    if ($LASTEXITCODE -eq 0 -and $existing -match [regex]::Escape($Id)) {
        return
    }
    winget install --id $Id -e --accept-package-agreements --accept-source-agreements
}

function Refresh-Path {
    $MachinePath = [System.Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [System.Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

Say "Installing Windows packages"
if (-not (Test-Command git)) { Install-WingetPackage "Git.Git" }
if (-not (Test-Command python)) { Install-WingetPackage "Python.Python.3.12" }
if (-not (Test-Command ffmpeg)) { Install-WingetPackage "Gyan.FFmpeg" }
Refresh-Path

Say "Syncing Buzzmoo Cooks from $RepoUrl"
if (Test-Path (Join-Path $AppDir ".git")) {
    git -C $AppDir fetch --all --prune
    git -C $AppDir pull --ff-only
} elseif (Test-Path $AppDir) {
    throw "Target exists and is not a git repo: $AppDir"
} else {
    git clone $RepoUrl $AppDir
}

Say "Creating pinned Python environment"
Set-Location $AppDir
$Python = if (Test-Command py) { "py" } else { "python" }
if ($Python -eq "py") {
    py -3.12 -m venv .venv
} else {
    python -m venv .venv
}
$VenvPython = Join-Path $AppDir ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip setuptools wheel
& $VenvPython -m pip install --upgrade --pre -r requirements.txt
& $VenvPython -m py_compile main.py

Say "Writing launcher"
$Launcher = Join-Path $AppDir "Start-BuzzmooCooks.ps1"
@"
Set-StrictMode -Version Latest
`$ErrorActionPreference = "Stop"
Set-Location "$AppDir"
& "$VenvPython" main.py
"@ | Set-Content -Path $Launcher -Encoding UTF8

try {
    $Desktop = [Environment]::GetFolderPath("Desktop")
    $ShortcutPath = Join-Path $Desktop "Buzzmoo Cooks.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = "powershell.exe"
    $Shortcut.Arguments = "-NoProfile -ExecutionPolicy Bypass -File `"$Launcher`""
    $Shortcut.WorkingDirectory = $AppDir
    $Shortcut.Save()
} catch {
    Warn "Desktop shortcut could not be created: $($_.Exception.Message)"
}

Say "Done"
Write-Host "Run it with:"
Write-Host "  powershell -ExecutionPolicy Bypass -File `"$Launcher`""
Write-Host ""
Write-Host "Native LiteRT-LM wheels may not be available for Windows yet. For Gemma-native inference, use WSL with install/linux.sh."
