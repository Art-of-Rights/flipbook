@echo off
rem Double-click to open the Flipbook library,
rem or drag one or more PDFs (or a folder of PDFs) onto this file to open them.
title Flipbook PDF Reader
cd /d "%~dp0"
set "PY=python"
if exist "%~dp0..\.venv\Scripts\python.exe" set "PY=%~dp0..\.venv\Scripts\python.exe"
"%PY%" serve.py %*
if errorlevel 1 pause
