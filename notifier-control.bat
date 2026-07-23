@echo off
REM ============================================================
REM  WorkBuddy 提醒器 一键开关入口（无黑框版）
REM  双击本文件：在「开启 / 关闭」之间切换，并弹出状态提示框。
REM  - 使用 pythonw.exe 运行，不会出现黑色控制台窗口（告别"闪退"观感）。
REM  - 关闭 = 结束 Python 进程（进程被杀，内存立即释放，占用为 0）
REM  - 开启 = 重新拉起进程，按 notifier_config.json 的模式监听
REM  也可带参数：
REM      notifier-control.bat on          强制开启
REM      notifier-control.bat off         强制关闭
REM      notifier-control.bat precise     切换为精准模式
REM      notifier-control.bat heuristic   切换为启发式模式
REM ============================================================
set "PY=C:\Users\LeviDIAO\.workbuddy\binaries\python\versions\3.13.12\pythonw.exe"
set "SCRIPT=D:\Projects\0-Temp\2026-07-23-19-26-54\workbuddy_wait_notifier.py"

if /i "%~1"=="on"        ( "%PY%" "%SCRIPT%" --on )
else if /i "%~1"=="off"  ( "%PY%" "%SCRIPT%" --off )
else if /i "%~1"=="precise"   ( "%PY%" "%SCRIPT%" --precise )
else if /i "%~1"=="heuristic"( "%PY%" "%SCRIPT%" --heuristic )
else                      ( "%PY%" "%SCRIPT%" --toggle )
exit /b
