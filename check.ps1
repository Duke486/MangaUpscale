$ErrorActionPreference = 'Stop'
Push-Location $PSScriptRoot
try {
    & './runtime/python/python.exe' -B -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed' }
    & './runtime/python/python.exe' -B backend/selftest.py
    if ($LASTEXITCODE -ne 0) { throw 'Synthetic pipeline test failed' }
} finally { Pop-Location }
