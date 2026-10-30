# AutoStock PowerShell 실행 스크립트
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "AutoStock - NH나무증권 시세포착 자동매매"

if (Test-Path ".\.venv\Scripts\python.exe") {
    & ".\.venv\Scripts\python.exe" main.py
} else {
    python main.py
}
