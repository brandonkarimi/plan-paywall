# Builds dist\PlanSender\PlanSender.exe (a folder you can zip and give to the engineer).
# Run from the project root:   powershell -ExecutionPolicy Bypass -File desktop\build.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

$py = ".venv\Scripts\python.exe"
& $py -m pip install -q pywebview pystray keyring stripe pypdfium2 pypdf pillow img2pdf pyinstaller
& $py desktop\make_icon.py

& $py -m PyInstaller run_desktop.py `
    --name PlanSender `
    --windowed `
    --onedir `
    --noconfirm `
    --clean `
    --icon build\PlanSender.ico `
    --add-data "desktop\ui;desktop\ui" `
    --hidden-import pystray._win32 `
    --hidden-import keyring.backends.Windows `
    --collect-all pypdfium2 `
    --collect-all pypdfium2_raw `
    --exclude-module django `
    --exclude-module reportlab `
    --exclude-module pytest `
    --exclude-module tkinter

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }
Compress-Archive -Path dist\PlanSender -DestinationPath dist\PlanSender.zip -Force
Write-Host ""
Write-Host "Built: dist\PlanSender\PlanSender.exe"
Write-Host "Zip to share: dist\PlanSender.zip"
