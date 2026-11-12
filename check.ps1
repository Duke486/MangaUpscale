$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & $Python -m ruff check .
    if ($LASTEXITCODE -ne 0) { throw 'Lint failed' }
    & $Python -m ruff format --check .
    if ($LASTEXITCODE -ne 0) { throw 'Formatting check failed' }
    & $Python -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed' }
    & $Python -B backend/selftest.py
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic pipeline test failed' }
    & $Python -B packaging/selftest.py
    if ($LASTEXITCODE -ne 0) { throw 'Launcher test failed' }
} finally { Pop-Location }
param([string]$Python = 'python')
