# Installs PaddleOCR into its own environment (.venv-ocr) for reading flyer images.
# About 1 GB on disk; the OCR models (~20 MB) download on first use.
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$venv = Join-Path $here '.venv-ocr'

if (-not (Test-Path "$venv\Scripts\python.exe")) { py -3.11 -m venv $venv }
& "$venv\Scripts\python.exe" -m pip install --upgrade pip
# paddlepaddle 3.2.x: 3.3's fast CPU path (oneDNN) crashes on Windows; 3.2 runs ~4x faster than 3.3 without it
& "$venv\Scripts\python.exe" -m pip install "paddlepaddle==3.2.2" paddleocr
Write-Host "PaddleOCR is ready. Choose 'PaddleOCR' in Settings > Read flyer images with."
