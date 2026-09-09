@echo off
setlocal
cd /d "%~dp0"

echo === Сборка fns-portal.exe ===
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python не найден. Установите Python 3.10+ с python.org
  echo и отметьте галочку "Add python.exe to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo Создаю виртуальное окружение .venv ...
  python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat

echo Ставлю зависимости и PyInstaller ...
python -m pip install -q --disable-pip-version-check -r requirements.txt || goto :error
python -m pip install -q --disable-pip-version-check pyinstaller || goto :error

echo Собираю ...
python -m PyInstaller fnsportal.spec --noconfirm --log-level WARN || goto :error

echo.
echo Готово: dist\fns-portal.exe
echo Скопируйте этот файл в любую папку, положите рядом выгрузки ФНС и запустите.
echo.
pause
goto :eof

:error
echo.
echo Ошибка на предыдущем шаге. Прочитайте сообщение выше.
pause
exit /b 1
