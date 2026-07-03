@echo off
:: can_logger_admin.bat - Self-elevating launcher for can_logger.py
::
:: can_logger.py requires administrator privileges to perform USB hardware resets
:: of ixxat dongles when a receive-buffer overflow latches the device.
::
:: Usage:
::   Double-click in Explorer, or run from any cmd prompt:
::   tools\can_logger_admin.bat [--output file.log] [--bitrate 500000] [--brands ixxat]
::
:: If not already elevated, a UAC prompt will appear. Click Yes to continue.
::
:: Note: cd /d into a network / cloud-synced path can fail for elevated
:: processes, so we avoid it entirely and run can_logger.py via its full 8.3
:: short path instead.

net session >nul 2>&1
if %errorlevel% == 0 goto :RUN

echo.
echo  Requesting administrator privileges...
echo  A UAC prompt will appear -- click Yes to continue.
echo.
powershell -Command "Start-Process cmd.exe -WorkingDirectory C:\Users\%USERNAME% -ArgumentList '/k python %~sdp0can_logger.py %*' -Verb RunAs"
exit /b

:RUN
python %~sdp0can_logger.py %*
