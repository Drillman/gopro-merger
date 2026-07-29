@echo off
setlocal
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"

rem Double-clic : menu interactif.
rem En ligne de commande : roulage.cmd rendu --session 15h53
python -m scripts %*

echo.
pause
