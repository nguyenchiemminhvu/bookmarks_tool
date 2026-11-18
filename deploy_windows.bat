@rem stop process that run tcp port 127.0.0.1:47213
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:47213"') do taskkill /PID %%a /F

@rem remove the current markbooks.exe if exists in dist folder
if exist "dist\bookmarks.exe" del /F "dist\bookmarks.exe"

@rem pack the exe file
python -m PyInstaller --onefile --noconsole --name "bookmarks" --add-data "bookmarks/static;bookmarks/static" run_bookmarks.py

@rem find the exe in dist folder
for %%f in (dist\*.exe) do set "EXE_PATH=%%~ff"
if not defined EXE_PATH (
    echo ERROR: no exe found in dist folder
    exit /b 1
)
for %%f in ("%EXE_PATH%") do set "EXE_DIR=%%~dpf"
echo Found exe: %EXE_PATH%

@rem make a shortcut from the exe, named as "bookmarks.lnk", at shell:startup folder, overwrite if exists
set SHORTCUT_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\bookmarks.lnk
if exist "%SHORTCUT_PATH%" del /F "%SHORTCUT_PATH%"
powershell -NoProfile -Command "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT_PATH%');$s.TargetPath='%EXE_PATH%';$s.WorkingDirectory='%EXE_DIR%';$s.Save()"

@rem run the exe file
start "" "%EXE_PATH%"
