# 打包为访客机可执行文件（Windows）
#
# 用法（在已激活的 venv 中）：
#   powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
#
# 产物：dist\vm_agent\vm_agent.exe + config.yaml

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install -q -r requirements.txt pyinstaller

python -m PyInstaller `
  --noconfirm `
  --clean `
  --name vm_agent `
  --onedir `
  --console `
  --paths $Root `
  --hidden-import=uiautomation `
  --hidden-import=comtypes `
  --hidden-import=win32api `
  --hidden-import=win32gui `
  --hidden-import=win32con `
  --hidden-import=pythoncom `
  --hidden-import=pywintypes `
  --collect-all uiautomation `
  --collect-all comtypes `
  main.py

Copy-Item -Force "$Root\config.yaml" "$Root\dist\vm_agent\config.yaml"

Write-Host ""
Write-Host "Build OK: $Root\dist\vm_agent\"
Write-Host "IMPORTANT: edit dist\vm_agent\config.yaml host to the HOST IP seen from the VM"
Write-Host "  NAT (VMnet8):       often 192.168.159.1"
Write-Host "  Host-Only (VMnet1): often 192.168.150.1"
Write-Host "Do NOT use 127.0.0.1 inside the guest."
Write-Host "Then copy the whole folder into the VM and run vm_agent.exe"
