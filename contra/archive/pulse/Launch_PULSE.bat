@echo off
:: PULSE LP Explorer — double-click to open (no terminal window version)
:: Hands off to Launch_PULSE.pyw which runs silently and opens your browser.

cd /d "%~dp0"
start "" pythonw.exe "%~dp0Launch_PULSE.pyw"
exit
