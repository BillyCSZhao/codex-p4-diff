[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ToolArgs
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$scriptPath = Join-Path $PSScriptRoot 'codex_p4_diff.py'
$candidates = @()

if ($env:USERPROFILE) {
    $candidates += [pscustomobject]@{
        Exe = (Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe')
        Prefix = @()
    }
}
$candidates += [pscustomobject]@{ Exe = 'py'; Prefix = @('-3') }
$candidates += [pscustomobject]@{ Exe = 'python'; Prefix = @() }
$candidates += [pscustomobject]@{ Exe = 'python3'; Prefix = @() }

$selected = $null
foreach ($candidate in $candidates) {
    $command = $candidate.Exe
    if (-not (Test-Path -LiteralPath $command) -and -not (Get-Command $command -ErrorAction SilentlyContinue)) {
        continue
    }
    try {
        $versionArgs = @($candidate.Prefix) + @(
            '-c',
            'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
        )
        & $command @versionArgs 2>$null
        if ($LASTEXITCODE -eq 0) {
            $selected = $candidate
            break
        }
    }
    catch {
        continue
    }
}

if (-not $selected) {
    [Console]::Error.WriteLine(
        'Codex P4 Diff requires Python 3.10+. Install it from https://www.python.org/downloads/ ' +
        'or make python/py -3 available, then restart Codex.'
    )
    exit 2
}

$env:PYTHONUTF8 = '1'
$invokeArgs = @($selected.Prefix) + @($scriptPath) + @($ToolArgs)
& $selected.Exe @invokeArgs
exit $LASTEXITCODE
