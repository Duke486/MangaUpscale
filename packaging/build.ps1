param([string]$SevenZip = '7z.exe')
$ErrorActionPreference='Stop'
$root=Split-Path $PSScriptRoot
$build=Join-Path $root 'build/client'
$dist=Join-Path $root 'dist/client'
New-Item -ItemType Directory -Force -Path $build,$dist | Out-Null
$seven=(Get-Command $SevenZip).Source
if (!(Test-Path (Join-Path (Split-Path $seven) '7z.dll'))) {
    if ((Split-Path $seven) -like '*shims') { $seven=Join-Path (Split-Path (Split-Path $seven)) 'apps/7zip/current/7z.exe' }
    else { $seven=Join-Path $env:ProgramFiles '7-Zip/7z.exe' }
}
foreach ($name in @('bootstrap.py','setup_client.py','gui.py','controller.py','preview_panel.py','trial_dialog.py','check.svg','runtime.json','README.md','THIRD_PARTY.md','licenses','backend')) {
    Copy-Item -LiteralPath (Join-Path $root $name) -Destination $build -Recurse -Force
}
& robocopy (Join-Path $root 'runtime/python') (Join-Path $build 'runtime/python') /E /R:1 /W:1 /NFL /NDL /NJH /NJS /XD site-packages __pycache__ include libs tcl Scripts /XF *.pyc *.pyo | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'Python copy failed' }
& robocopy (Join-Path $root 'runtime/nunif') (Join-Path $build 'runtime/nunif') /E /R:1 /W:1 /NFL /NDL /NJH /NJS /XD __pycache__ .git /XF *.pyc *.pyo AGENTS.md | Out-Null
if ($LASTEXITCODE -gt 7) { throw 'nunif copy failed' }
$tools=Join-Path $build 'runtime/tools'
New-Item -ItemType Directory -Force -Path $tools | Out-Null
Copy-Item -LiteralPath $seven,(Join-Path (Split-Path $seven) '7z.dll') -Destination $tools
$payload=Join-Path $dist 'client.7z'
if (Test-Path -LiteralPath $payload) { Remove-Item -LiteralPath $payload }
Push-Location $build
try { & $seven a -t7z $payload . -mx=3 -mmt=2 '-xr!__pycache__' '-xr!*.pyc'; if ($LASTEXITCODE -ne 0) { throw 'Client compression failed' } } finally { Pop-Location }
$hash=(Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash.ToLowerInvariant()
$source=(Get-Content (Join-Path $PSScriptRoot 'Launcher.cs') -Raw).Replace('PAYLOAD_HASH',$hash).Replace('PAYLOAD_PARTS','')
$generated=Join-Path $dist 'Launcher.cs'
[IO.File]::WriteAllText($generated,$source,[Text.UTF8Encoding]::new($false))
$stub=Join-Path $dist 'stub.exe'
& "$env:WINDIR/Microsoft.NET/Framework64/v4.0.30319/csc.exe" /nologo /target:winexe /platform:x64 /optimize+ /debug- /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/resource:$seven,7z.exe" "/resource:$(Join-Path (Split-Path $seven) '7z.dll'),7z.dll" "/out:$stub" $generated
if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
$output=Join-Path $dist 'MangaUpscale-5.0.0-Windows-x64.exe'
$stream=[IO.File]::Create($output)
try {
    foreach ($file in @($stub,$payload)) { $inputFile=[IO.File]::OpenRead($file); try { $inputFile.CopyTo($stream) } finally { $inputFile.Dispose() } }
    $footer=[BitConverter]::GetBytes([long](Get-Item -LiteralPath $payload).Length)
    $stream.Write($footer,0,8)
} finally { $stream.Dispose() }
$digest=(Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
"$digest  $(Split-Path $output -Leaf)" | Set-Content (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
Get-Item -LiteralPath $output | Select-Object Name,Length
