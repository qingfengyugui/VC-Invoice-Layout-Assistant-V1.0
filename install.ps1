param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('codex', 'claude-code', 'openclaw', 'workbuddy', 'qoder', 'qclaw')]
    [string]$Platform,
    [string]$Destination,
    [string]$Repository = 'qingfengyugui/invoice-layout-agent',
    [switch]$Force,
    [switch]$SkipMcp
)

$ErrorActionPreference = 'Stop'
$userProfile = [Environment]::GetFolderPath('UserProfile')
$localData = [Environment]::GetFolderPath('LocalApplicationData')
$platformRoots = @{
    'codex' = Join-Path $userProfile '.agents\skills'
    'claude-code' = Join-Path $userProfile '.claude\skills'
    'openclaw' = Join-Path $userProfile '.openclaw\skills'
    'workbuddy' = Join-Path $userProfile '.claude\skills'
    'qoder' = Join-Path $userProfile '.qoder\skills'
    'qclaw' = Join-Path $userProfile '.qclaw\skills'
}
if (-not $Destination) { $Destination = $platformRoots[$Platform] }
$assetName = 'invoice-layout-agent-windows-x64.zip'
$releaseBase = "https://github.com/$Repository/releases/latest/download"
$taskInstallRoot = Join-Path ([IO.Path]::GetTempPath()) ('invoice-layout-install-' + [guid]::NewGuid().ToString('N'))
$archive = Join-Path $taskInstallRoot $assetName
$checksums = Join-Path $taskInstallRoot 'SHA256SUMS'
$expanded = Join-Path $taskInstallRoot 'expanded'
New-Item -ItemType Directory -Path $taskInstallRoot | Out-Null

try {
    Invoke-WebRequest -Uri "$releaseBase/$assetName" -OutFile $archive
    Invoke-WebRequest -Uri "$releaseBase/SHA256SUMS" -OutFile $checksums
    $line = Get-Content -LiteralPath $checksums | Where-Object { $_ -match "\s$([regex]::Escape($assetName))$" } | Select-Object -First 1
    if (-not $line) { throw "Checksum entry missing for $assetName" }
    $expected = ($line -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw 'Runtime bundle checksum mismatch.' }
    Expand-Archive -LiteralPath $archive -DestinationPath $expanded

    $installBase = Join-Path $localData 'invoice-layout-agent'
    $runtimeRoot = Join-Path $installBase 'current'
    New-Item -ItemType Directory -Force -Path $installBase | Out-Null
    if (Test-Path -LiteralPath $runtimeRoot) {
        if (-not $Force) { throw "Runtime already exists: $runtimeRoot. Re-run with -Force to upgrade." }
        $backup = Join-Path $installBase ('backup-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
        Move-Item -LiteralPath $runtimeRoot -Destination $backup
    }
    Move-Item -LiteralPath $expanded -Destination $runtimeRoot

    $shimRoot = Join-Path $installBase 'bin'
    New-Item -ItemType Directory -Force -Path $shimRoot | Out-Null
    $executable = Join-Path $runtimeRoot 'invoice-layout.exe'
    $shim = Join-Path $shimRoot 'invoice-layout.cmd'
    Set-Content -LiteralPath $shim -Encoding ascii -Value "@echo off`r`n`"$executable`" %*`r`n"
    $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
    $pathParts = @($userPath -split ';' | Where-Object { $_ })
    if ($shimRoot -notin $pathParts) {
        [Environment]::SetEnvironmentVariable('Path', (($pathParts + $shimRoot) -join ';'), 'User')
    }
    $env:Path = "$shimRoot;$env:Path"

    $skillRoot = [IO.Path]::GetFullPath($Destination)
    $skillDestination = Join-Path $skillRoot 'invoice-layout-agent'
    New-Item -ItemType Directory -Force -Path $skillRoot | Out-Null
    if (Test-Path -LiteralPath $skillDestination) {
        if (-not $Force) { throw "Skill already exists: $skillDestination. Re-run with -Force to upgrade." }
        $skillBackup = "$skillDestination.backup-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
        Move-Item -LiteralPath $skillDestination -Destination $skillBackup
    }
    Copy-Item -LiteralPath (Join-Path $runtimeRoot "platforms\$Platform\invoice-layout-agent") -Destination $skillDestination -Recurse
    Set-Content -LiteralPath (Join-Path $skillDestination 'RUNTIME.md') -Encoding utf8 -Value "Use this complete runtime executable for every command:`n`n``$executable``"

    if (-not $SkipMcp) {
        if ($Platform -eq 'codex' -and (Get-Command codex -ErrorAction SilentlyContinue)) {
            & codex mcp add invoice-layout -- $executable mcp
        } elseif ($Platform -in @('claude-code', 'workbuddy') -and (Get-Command claude -ErrorAction SilentlyContinue)) {
            & claude mcp add --transport stdio invoice-layout -- $executable mcp
        } elseif ($Platform -eq 'qoder' -and (Get-Command qodercli -ErrorAction SilentlyContinue)) {
            & qodercli mcp add invoice-layout -- $executable mcp
        }
    }

    & $executable doctor
    Write-Host "Installed complete runtime: $runtimeRoot"
    Write-Host "Installed Skill: $skillDestination"
    Write-Host 'No Python, Java, WPS, Poppler, OCR, Maven, Docker, or archive-tool installation is required.'
} finally {
    if (Test-Path -LiteralPath $taskInstallRoot) {
        Remove-Item -LiteralPath $taskInstallRoot -Recurse -Force
    }
}
