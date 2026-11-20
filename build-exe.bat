@echo off
rem Build "2NK INCI Check.exe" (one file) -> inci-check\dist\
rem Usage: build-exe.bat [path\to\python.exe]
rem needs: pip install pymupdf pywebview pyinstaller rapidfuzz openpyxl rapidocr onnxruntime pillow
cd /d "%~dp0"
set PY=%~1
if "%PY%"=="" set PY=python
rem build outside Dropbox (Dropbox locks files while PyInstaller writes them)
set BUILD=%TEMP%\2nkInciBuild
"%PY%" tools\make_icon.py || exit /b 1
"%PY%" tools\prepare_models.py "%BUILD%\models" || exit /b 1
"%PY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "2NK INCI Check" --icon "%~dp0icon.ico" ^
  --add-data "%~dp0ui;ui" --add-data "%~dp0data\regdb.json;data" --add-data "%~dp0data\regs;data\regs" ^
  --add-data "%BUILD%\rapid\config.yaml;rapidocr" --add-data "%BUILD%\rapid\default_models.yaml;rapidocr" ^
  --add-data "%BUILD%\rapid\nets\arch_config.yaml;rapidocr\inference_engine\pytorch\networks" ^
  --add-data "%BUILD%\models;rapidocr\models" --collect-submodules rapidocr ^
  --hidden-import openpyxl --exclude-module matplotlib --exclude-module tkinter ^
  --workpath "%BUILD%\work" --distpath "%BUILD%\dist" --specpath "%BUILD%" app\main.py || exit /b 1
if not exist dist mkdir dist
copy /y "%BUILD%\dist\2NK INCI Check.exe" dist\ || exit /b 1
echo Done: dist\2NK INCI Check.exe
