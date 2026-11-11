param([string]$SevenZip = '7z.exe', [switch]$ReusePayload)
$ErrorActionPreference = 'Stop'
$root = Split-Path $PSScriptRoot
$build = Join-Path $root 'build'
$dist = Join-Path $root 'dist'
New-Item -ItemType Directory -Force -Path $build,$dist | Out-Null
$payload = Join-Path $build 'payload.7z'
$seven = (Get-Command $SevenZip).Source
if ((Split-Path $seven) -like '*shims') {
    $seven = Join-Path (Split-Path (Split-Path $seven)) 'apps/7zip/current/7z.exe'
}
if (!(Test-Path (Join-Path (Split-Path $seven) '7z.dll'))) {
    $seven = Join-Path $env:ProgramFiles '7-Zip/7z.exe'
}
if (!(Test-Path (Join-Path (Split-Path $seven) '7z.dll'))) { throw '7z.dll must be beside 7z.exe' }
Push-Location $root
try {
    if (!$ReusePayload) {
        if (Test-Path -LiteralPath $payload) { Remove-Item -LiteralPath $payload }
        $files = @('bootstrap.py','gui.py','controller.py','preview_panel.py','trial_dialog.py','check.svg','runtime.json','README.md','THIRD_PARTY.md','licenses','backend','runtime','ui_runtime')
        & $seven a -t7z $payload @files -mx=7 -mmt=2 -ms=256m -md=64m '-xr!__pycache__' '-xr!*.pyc' '-xr!.git' '-xr!AGENTS.md' '-xr!*.engine' '-xr!*.log' '-xr!*.tmp'
        if ($LASTEXITCODE -ne 0) { throw 'Runtime compression failed' }
    }
    $hash = (Get-FileHash -LiteralPath $payload -Algorithm SHA256).Hash.ToLowerInvariant()
    $large = (Get-Item -LiteralPath $payload).Length -ge (2GB - 8MB)
    $parts = @()
    if ($large) {
        $inputArchive = [IO.File]::OpenRead($payload)
        try {
            $buffer = [byte[]]::new(1MB)
            $index = 1
            while ($inputArchive.Position -lt $inputArchive.Length) {
                $part = Join-Path $dist ('MangaUpscale-5.0.0-runtime.7z.{0:000}' -f $index)
                $partStream = [IO.File]::Create($part)
                try {
                    $remaining = 1GB
                    while ($remaining -gt 0) {
                        $read = $inputArchive.Read($buffer,0,[Math]::Min($buffer.Length,$remaining))
                        if (!$read) { break }
                        $partStream.Write($buffer,0,$read)
                        $remaining -= $read
                    }
                } finally { $partStream.Dispose() }
                $parts += (Get-FileHash -LiteralPath $part -Algorithm SHA256).Hash.ToLowerInvariant()
                $index++
            }
        } finally { $inputArchive.Dispose() }
    }
    $source = (Get-Content (Join-Path $PSScriptRoot 'Launcher.cs') -Raw).Replace('PAYLOAD_HASH', $hash).Replace('PAYLOAD_PARTS', ($parts -join ','))
    $generated = Join-Path $build 'Launcher.cs'
    [IO.File]::WriteAllText($generated, $source, [Text.UTF8Encoding]::new($false))
    $stub = Join-Path $build 'Launcher.exe'
    $compiler = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
    & $compiler /nologo /target:winexe /platform:x64 /optimize+ /debug- /reference:System.Windows.Forms.dll /reference:System.Drawing.dll "/resource:$seven,7z.exe" "/resource:$(Join-Path (Split-Path $seven) '7z.dll'),7z.dll" "/out:$stub" $generated
    if ($LASTEXITCODE -ne 0) { throw 'Launcher compilation failed' }
    $name = if ($large) { 'MangaUpscale-5.0.0-Windows-x64-offline.exe' } else { 'MangaUpscale-5.0.0-Windows-x64.exe' }
    $output = Join-Path $dist $name
    $destination = [IO.File]::Create($output)
    try {
        foreach ($path in @($stub,$payload)) {
            $inputFile = [IO.File]::OpenRead($path)
            try { $inputFile.CopyTo($destination) } finally { $inputFile.Dispose() }
        }
        $footer = [BitConverter]::GetBytes([long](Get-Item -LiteralPath $payload).Length)
        $destination.Write($footer,0,$footer.Length)
    } finally { $destination.Dispose() }
    if ($large) {
        $output = Join-Path $dist 'MangaUpscale-5.0.0-Windows-x64.exe'
        Copy-Item -LiteralPath $stub -Destination $output -Force
        $online = [IO.File]::OpenWrite($output)
        try {
            $online.Seek(0,[IO.SeekOrigin]::End) | Out-Null
            $footer = [BitConverter]::GetBytes([long]0)
            $online.Write($footer,0,$footer.Length)
        } finally { $online.Dispose() }
    }
    $digest = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
    "$digest  $(Split-Path $output -Leaf)" | Set-Content (Join-Path $dist 'SHA256SUMS.txt') -Encoding ascii
    Get-Item -LiteralPath $output | Select-Object Name,Length
} finally { Pop-Location }
