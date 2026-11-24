# SPDX-FileCopyrightText: 2026 Goo Engine Authors
#
# SPDX-License-Identifier: GPL-2.0-or-later
#
# Sign Windows release artifacts with signtool.
#
# SAFETY: this script is a DRY-RUN by default. It prints the signing and
# verification commands it would run and exits 0. Pass -Execute to actually
# sign. No certificate password is ever written to output or logs; the
# password is read from the environment variable named by
# -CertificatePasswordEnv (default GOO_WIN_CERT_PASSWORD).
#
# Examples:
#   pwsh -File tools/release/sign_windows.ps1 -ArtifactsDir dist/5.2.0
#   $env:GOO_WIN_CERT_PATH = 'C:\secrets\goo.pfx'
#   $env:GOO_WIN_CERT_PASSWORD = '***'
#   pwsh -File tools/release/sign_windows.ps1 -ArtifactsDir dist/5.2.0 -Execute
#
# Sign BEFORE tools/release/build_release.py so SHA256SUMS covers signed bytes.
# signtool is optional: with -Execute and no tool/certificate the script fails
# loudly instead of silently producing unsigned artifacts.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$ArtifactsDir,

    [string]$CertificatePath = $env:GOO_WIN_CERT_PATH,

    [string]$CertificatePasswordEnv = 'GOO_WIN_CERT_PASSWORD',

    [string]$TimestampUrl = 'http://timestamp.digicert.com',

    [string]$SigntoolPath = $env:SIGTOOL_PATH,

    [switch]$Execute
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-SignTool {
    param([string]$ExplicitPath)

    if ($ExplicitPath) {
        if (Test-Path -LiteralPath $ExplicitPath -PathType Leaf) {
            return (Resolve-Path -LiteralPath $ExplicitPath).Path
        }
        throw "signtool not found at SIGTOOL_PATH / -SigntoolPath: $ExplicitPath"
    }
    $command = Get-Command signtool.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }
    $kits = @()
    foreach ($base in @(${env:ProgramFiles(x86)}, $env:ProgramFiles)) {
        if (-not $base) { continue }
        $kit = Join-Path $base 'Windows Kits\10\bin'
        if (Test-Path -LiteralPath $kit -PathType Container) {
            $kits += $kit
        }
    }
    foreach ($kit in $kits) {
        $candidates = Get-ChildItem -LiteralPath $kit -Directory -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending |
            ForEach-Object { Join-Path $_.FullName 'x64\signtool.exe' } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf }
        if ($candidates) {
            return $candidates[0]
        }
    }
    return $null
}

if (-not (Test-Path -LiteralPath $ArtifactsDir -PathType Container)) {
    Write-Host "ERROR: ArtifactsDir does not exist: $ArtifactsDir" -ForegroundColor Red
    exit 2
}

$artifactRoot = (Resolve-Path -LiteralPath $ArtifactsDir).Path
$extensions = @('.exe', '.msi', '.dll', '.sys')
$files = @(Get-ChildItem -LiteralPath $artifactRoot -Recurse -File -ErrorAction Stop |
    Where-Object { $extensions -contains $_.Extension.ToLowerInvariant() } |
    Sort-Object FullName)

Write-Host "sign_windows.ps1"
Write-Host "  artifacts: $artifactRoot"
Write-Host "  files: $($files.Count)"
Write-Host "  mode: $(if ($Execute) { 'EXECUTE (real signing)' } else { 'DRY-RUN (default)' })"

if ($files.Count -eq 0) {
    Write-Warning "no .exe/.msi/.dll/.sys files found below $artifactRoot"
    exit 0
}

$signtool = Resolve-SignTool -ExplicitPath $SigntoolPath
if (-not $signtool) {
    if ($Execute) {
        Write-Host "ERROR: signtool.exe not found. Install the Windows SDK or set SIGTOOL_PATH / -SigntoolPath." -ForegroundColor Red
        exit 2
    }
    Write-Warning "signtool.exe not found; dry-run will print commands without a resolved tool path"
    $signtool = '<signtool-not-found>'
}

$password = $null
$displayCertificate = if ($CertificatePath) { $CertificatePath } else { '<certificate-not-set>' }
if ($Execute) {
    if (-not $CertificatePath) {
        Write-Host "ERROR: CertificatePath is empty. Set GOO_WIN_CERT_PATH or pass -CertificatePath." -ForegroundColor Red
        exit 2
    }
    $CertificatePath = (Resolve-Path -LiteralPath $CertificatePath).Path
    $password = [Environment]::GetEnvironmentVariable($CertificatePasswordEnv)
    if (-not $password) {
        Write-Host "ERROR: certificate password environment variable '$CertificatePasswordEnv' is empty." -ForegroundColor Red
        exit 2
    }
}

$failures = 0
foreach ($file in $files) {
    $arguments = @('sign', '/fd', 'SHA256', '/f', $CertificatePath, '/p', '<redacted>')
    if ($TimestampUrl) {
        $arguments += @('/tr', $TimestampUrl, '/td', 'SHA256')
    }
    $arguments += $file.FullName
    $printable = @('sign', '/fd', 'SHA256', '/f', $displayCertificate, '/p', '<redacted>')
    if ($TimestampUrl) {
        $printable += @('/tr', $TimestampUrl, '/td', 'SHA256')
    }
    $printable += $file.FullName

    if (-not $Execute) {
        Write-Host ("  [dry-run] & `"{0}`" {1}" -f $signtool, ($printable -join ' '))
        Write-Host ("  [dry-run] & `"{0}`" verify /pa /q `"{1}`"" -f $signtool, $file.FullName)
        continue
    }

    Write-Host "  signing: $($file.FullName)"
    try {
        & $signtool @arguments | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "signtool sign exited $LASTEXITCODE"
        }
        & $signtool verify /pa /q $file.FullName | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "signtool verify exited $LASTEXITCODE"
        }
        Write-Host "  verified: $($file.Name)"
    }
    catch {
        $failures++
        Write-Host "  ERROR: signing failed for $($file.FullName): $_" -ForegroundColor Red
    }
}

if (-not $Execute) {
    Write-Host "dry-run: no signing performed. Re-run with -Execute to sign for real."
    Write-Host "remember: sign before build_release.py so SHA256SUMS covers signed bytes."
    exit 0
}

if ($failures -gt 0) {
    Write-Host "ERROR: $failures file(s) failed signing or verification" -ForegroundColor Red
    exit 1
}
Write-Host "signed and verified $($files.Count) file(s)."
exit 0
