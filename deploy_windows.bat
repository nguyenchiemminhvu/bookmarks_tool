@rem stop process that run tcp port 127.0.0.1:47213
for /f "tokens=5" %%a in ('netstat -ano ^| findstr "127.0.0.1:47213"') do taskkill /PID %%a /F

@rem remove the current markbooks.exe if exists in dist folder
if exist "dist\bookmarks.exe" del /F "dist\bookmarks.exe"

@rem pack the exe file
python -m PyInstaller --onefile --noconsole --name "bookmarks" --add-data "bookmarks/static;bookmarks/static" run_bookmarks.py

@rem find the exe in dist folder
for %%f in (dist\*.exe) do set EXE_PATH=%%f
echo Found exe: %EXE_PATH%

@rem make a shortcut next to the exe, named as "bookmarks.lnk", overwrite if exists
set SHORTCUT_PATH=%~dp0bookmarks.lnk
powershell "$s=(New-Object -COM WScript.Shell).CreateShortcut('%SHORTCUT_PATH%');$s.TargetPath='%EXE_PATH%';$s.Save()"
if exist "%SHORTCUT_PATH%" del /F "%SHORTCUT_PATH%"

@rem move the shortcut to the shell:startup folder, overwrite if exists
set STARTUP_PATH=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\bookmarks.lnk
move /Y /F "%SHORTCUT_PATH%" "%STARTUP_PATH%"

@rem run the exe file
start "" "%EXE_PATH%"
