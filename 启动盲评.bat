@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0" || exit /b 1

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" "src\evaluateandscore\server.py" --open-browser %*
) else (
    uv run --no-project "src\evaluateandscore\server.py" --open-browser %*
)

set "survey_exit=%errorlevel%"
popd
if not "%survey_exit%"=="0" (
    echo 启动或运行出错，请查看上方提示。
    pause
)
exit /b %survey_exit%
