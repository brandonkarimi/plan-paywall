# Builds dist\PlanSender\PlanSender.exe and dist\PlanSender.zip.
# Run from the project root:  powershell -ExecutionPolicy Bypass -File desktop\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = ".venv\Scripts\python.exe"
& $py -m pip install -q -r requirements-dev.txt
& $py desktop\make_icon.py

& $py -m PyInstaller run_desktop.py `
    --name PlanSender --windowed --onedir --noconfirm --clean `
    --icon build\PlanSender.ico `
    --add-data "desktop\ui;desktop\ui" `
    --hidden-import keyring.backends.Windows `
    --collect-all pypdfium2 --collect-all pypdfium2_raw `
    --exclude-module reportlab --exclude-module pytest --exclude-module tkinter
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

Start-Sleep -Seconds 2  # let antivirus finish scanning new files before zipping
Compress-Archive -Path dist\PlanSender -DestinationPath dist\PlanSender.zip -Force
Write-Host "Built dist\PlanSender\PlanSender.exe and dist\PlanSender.zip"
