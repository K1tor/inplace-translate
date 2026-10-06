@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul && (set "PY=python") || (set "PY=py -3")

if "%~1"=="" (
    echo ================================================
    echo   InPlace Translate  原地翻译
    echo.
    echo   把要翻译的文件或文件夹拖到本 bat 图标上即可。
    echo   命令行用法: python translator.py 文件 -t zh-CN
    echo ================================================
    pause
    exit /b
)

set "TARGET="
set /p TARGET=目标语言(回车默认 zh-CN, 可填 en/ja/ko/zh-TW...): 
if "%TARGET%"=="" set "TARGET=zh-CN"
echo.
echo 正在翻译为 %TARGET% ...
%PY% "%~dp0translator.py" %* -t %TARGET%
echo.
pause
