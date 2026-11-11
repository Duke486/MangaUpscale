param([switch]$StartupTest)
$ErrorActionPreference = 'Stop'
$pythonWindow = Join-Path $PSScriptRoot 'runtime/python/pythonw.exe'
$launchArgs = @('-B', ('"' + (Join-Path $PSScriptRoot 'bootstrap.py') + '"'))
if ($StartupTest) { $launchArgs += '--startup-test' }
Start-Process -FilePath $pythonWindow -ArgumentList $launchArgs -WorkingDirectory $PSScriptRoot
