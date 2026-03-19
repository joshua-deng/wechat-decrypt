import platform
import os
import subprocess

def isRunningOnWsl():
    return (
        platform.system().lower() == "linux"
        and os.path.exists('/proc/sys/fs/binfmt_misc/WSLInterop')
    )

def convertWindowsPath2Wsl(windowsPath):
    result = subprocess.run(['wslpath', '-u', windowsPath], capture_output=True, text=True, check=True)
    return result.stdout.strip()