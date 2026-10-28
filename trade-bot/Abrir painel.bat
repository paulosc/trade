@echo off
rem Abre o painel do trade-bot no navegador (dois cliques).
cd /d "%~dp0"
where python >nul 2>nul || (echo Python nao encontrado. Instale em https://www.python.org/downloads/ e marque "Add python.exe to PATH". & pause & exit /b 1)
python -c "import zoneinfo; zoneinfo.ZoneInfo('America/New_York')" >nul 2>nul || (echo Instalando o fuso horario de Nova York ^(tzdata^)... & python -m pip install --user --quiet tzdata)
set PYTHONIOENCODING=utf-8
python -m tradebot painel
pause
