# 打包为访客机可执行文件（Windows）
#
# 用法（在已激活的 venv 中）：
#   powershell -ExecutionPolicy Bypass -File .\scripts\build_exe.ps1
#
# 产物：dist\vm_agent\vm_agent.exe + config.yaml
#
# 注意：若 venv 基于 Miniconda/Anaconda，PyInstaller 默认找不到
# Library\bin 下的 ffi.dll / libssl 等，会导致访客机报
#   ImportError: DLL load failed while importing _ctypes
# 本脚本会把 conda Library\bin 加入 PATH，并在打包后补拷缺失 DLL。

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

python -m pip install -q -r requirements.txt pyinstaller

# conda 基座：venv 的 sys.base_prefix → …\envs\xxx\Library\bin
$CondaBin = python -c @"
import sys
from pathlib import Path
p = Path(sys.base_prefix) / 'Library' / 'bin'
print(p if p.is_dir() else '')
"@
$CondaBin = ($CondaBin | Out-String).Trim()
if ($CondaBin) {
    Write-Host "Conda DLL path: $CondaBin"
    $env:PATH = "$CondaBin;$env:PATH"
} else {
    Write-Host "No conda Library\bin detected (ok for official CPython)."
}

python -m PyInstaller `
  --noconfirm `
  --clean `
  --name vm_agent `
  --onedir `
  --console `
  --noupx `
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

# 兜底：即便分析阶段仍漏掉，也把 conda 运行时 DLL 拷进 _internal
$Internal = Join-Path $Root "dist\vm_agent\_internal"
if ($CondaBin -and (Test-Path $Internal)) {
    $Need = @(
        "ffi.dll",
        "libffi-8.dll",
        "libssl-3-x64.dll",
        "libcrypto-3-x64.dll",
        "libexpat.dll",
        "liblzma.dll",
        "libbz2.dll",
        "LIBBZ2.dll"
    )
    foreach ($name in $Need) {
        $src = Join-Path $CondaBin $name
        if (Test-Path $src) {
            Copy-Item -Force $src (Join-Path $Internal $name)
            Write-Host "Bundled: $name"
        }
    }
}

$Ffi = Join-Path $Internal "ffi.dll"
if (-not (Test-Path $Ffi)) {
    Write-Host ""
    Write-Host "WARNING: ffi.dll missing in $Internal"
    Write-Host "  Guest VMs will fail with: DLL load failed while importing _ctypes"
    Write-Host "  Prefer building with official python.org CPython, or ensure conda Library\bin exists."
}

Write-Host ""
Write-Host "Build OK: $Root\dist\vm_agent\"
Write-Host "IMPORTANT: edit dist\vm_agent\config.yaml host to the HOST IP seen from the VM"
Write-Host "  NAT (VMnet8):       often 192.168.159.1"
Write-Host "  Host-Only (VMnet1): often 192.168.150.1"
Write-Host "Do NOT use 127.0.0.1 inside the guest."
Write-Host "Then copy the whole folder into the VM and run vm_agent.exe"
