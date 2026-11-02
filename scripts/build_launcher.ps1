$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (!(Test-Path -LiteralPath $compiler)) {
    throw 'The Windows .NET Framework C# compiler was not found.'
}
& $compiler /nologo /target:winexe /reference:System.Windows.Forms.dll "/win32icon:$projectRoot\pc_monitor.ico" "/out:$projectRoot\PC Monitor Launcher.exe" "$projectRoot\launcher\MonitorLauncher.cs"
if ($LASTEXITCODE -ne 0) { throw 'Launcher build failed.' }
Write-Output 'Created PC Monitor Launcher.exe. Create a desktop shortcut to this file.'
