@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите Python 3.9+ с python.org
  echo и отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Создаю виртуальное окружение .venv ...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat

echo Проверяю зависимости ...
python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :error

if not exist data\db\fns.sqlite (
  echo Базы нет: создаю справочники и демонстрационные данные ...
  python -m fnsportal init || goto :error
  python -m fnsportal demo || goto :error
  python -m fnsportal build-terms || goto :error
)

echo.
echo Портал запускается: http://127.0.0.1:8000
echo Остановить - Ctrl+C
echo.
python -m fnsportal serve %*
goto :eof

:error
echo.
echo Ошибка на предыдущем шаге. Прочитайте сообщение выше.
pause
exit /b 1
