[CmdletBinding()]
param(
    [string]$BaseRef
)

# Windows canonical gate: unit tests, bilingual doc parity, Markdown relative links, whitespace.
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot

function Invoke-Gate {
    param([string]$Name, [scriptblock]$Command)
    Write-Host "==> $Name"
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "$Name failed with exit code $LASTEXITCODE." }
}

$python = @('python', 'python3') | Where-Object { Get-Command $_ -ErrorAction SilentlyContinue } | Select-Object -First 1
if (-not $python) { throw 'python is required on PATH.' }

Invoke-Gate 'Unit tests' { & $python -m unittest discover -s (Join-Path $repoRoot 'tests') -p 'test_*.py' -v }
Invoke-Gate 'Bilingual doc parity' { & $python (Join-Path $repoRoot 'tools/check_doc_parity.py') }
Invoke-Gate 'Markdown relative links' { & $python (Join-Path $repoRoot 'tools/check_links.py') }
Invoke-Gate 'git diff --check (unstaged)' { git -C $repoRoot diff --check }
Invoke-Gate 'git diff --cached --check (staged)' { git -C $repoRoot diff --cached --check }

if (-not [string]::IsNullOrWhiteSpace($BaseRef) -and $BaseRef -notmatch '^0+$') {
    $baseCommit = (& git -C $repoRoot rev-parse --verify "$BaseRef^{commit}" 2>$null | Select-Object -First 1)
    if ($LASTEXITCODE -eq 0 -and $baseCommit) {
        Invoke-Gate "git diff --check $baseCommit..HEAD" { git -C $repoRoot diff --check "$baseCommit..HEAD" }
    }
}
Write-Host 'dev_check: all gates passed'
exit 0
