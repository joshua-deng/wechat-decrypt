@echo off
chcp 65001 >nul
echo ========================================
echo   WeChatDecrypt 打包脚本 (Web UI + CLI 双模式)
echo ========================================
echo.

REM 检查 pyinstaller
where pyinstaller >nul 2>&1
if errorlevel 1 (
    echo [!] 未找到 pyinstaller, 正在安装...
    pip install pyinstaller
)

echo [*] 调用 PyInstaller (走 WeChatDecrypt.spec, 不重复维护文件清单)...
echo.

pyinstaller --noconfirm WeChatDecrypt.spec

if errorlevel 1 (
    echo.
    echo [!] 打包失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   打包完成!
echo   输出: dist\WeChatDecrypt.exe
for %%F in (dist\WeChatDecrypt.exe) do echo   大小: %%~zF bytes
echo.
echo   使用: 双击 WeChatDecrypt.exe
echo   ^→ 自动启动浏览器到 http://localhost:5678 (Web UI)
echo   CLI: WeChatDecrypt.exe --help
echo ========================================
echo.
pause
