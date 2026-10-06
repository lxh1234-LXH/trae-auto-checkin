@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo [1/4] 安装打包依赖...
python -m pip install --upgrade pyinstaller
if errorlevel 1 goto :fail

echo.
echo [2/4] 开始打包...
pyinstaller --noconfirm --clean --onedir --windowed ^
  --name checkin ^
  --add-data "templates;templates" ^
  --add-data "flows.json;." ^
  --collect-submodules psutil ^
  --exclude-module matplotlib ^
  --exclude-module scipy ^
  --exclude-module pandas ^
  --exclude-module win32com ^
  --exclude-module pythonwin ^
  --exclude-module pywin ^
  --exclude-module tkinter.test ^
  checkin.py
if errorlevel 1 goto :fail

echo.
echo [3/4] 清理用不到的大文件...
del /q "dist\checkin\_internal\cv2\opencv_videoio_ffmpeg*.dll" 2>nul
rmdir /s /q "dist\checkin\_internal\Pythonwin" 2>nul

echo.
echo [4/4] 完成，输出目录：%cd%\dist\checkin
echo 把整个 dist\checkin 文件夹压缩后发给别人，对方解压后双击 checkin.exe 即可。
pause
exit /b 0

:fail
echo.
echo 打包失败，请把上面的报错发给开发者。
pause
exit /b 1
