param([string]$Python = "python")
$ErrorActionPreference = "Stop"
Push-Location (Join-Path $PSScriptRoot "..")
try {
    & $Python -c "import platform, struct, sys; sys.exit(sys.platform != 'win32' or platform.machine().upper() != 'AMD64' or struct.calcsize('P') != 8)"
    if ($LASTEXITCODE -ne 0) { throw "Build with 64-bit Python on Windows x64." }
    & $Python -m PyInstaller --noconfirm --clean --onefile --windowed --noupx `
        --name DMC600-Reverse-Windows-x64 `
        --collect-submodules bleak.backends.winrt `
        --add-data "samples/demo.csv:samples" src/meter_dashboard.py
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }
} finally {
    Pop-Location
}
