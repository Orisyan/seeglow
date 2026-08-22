# 拾光 SeeGlow · 命令行 EXE 一键打包脚本
# 用法：pip install pyinstaller 后，在项目根目录执行 .\build_exe.ps1
# 产物：dist\SeeGlow.exe（免 Python 环境，双击即用）

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m PyInstaller --name SeeGlow --onefile --console --clean `
  --paths . `
  --icon icon.ico `
  --exclude-module fastapi --exclude-module uvicorn --exclude-module pydantic `
  exe_entry.py

if ($LASTEXITCODE -eq 0) {
  Write-Host ""
  Write-Host "打包完成：dist\SeeGlow.exe" -ForegroundColor Green
  Write-Host "提示：EXE 同目录的 config.json 存放 API 配置，拾光\ 文件夹存放总结结果。"
} else {
  Write-Host "打包失败，请检查上方报错。" -ForegroundColor Red
}
