@echo off
title EPIS
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_epis.ps1"
if errorlevel 1 pause
