<#
.SYNOPSIS
    Create a throw-away self-signed Authenticode demo signature for a Windows setup.

.DESCRIPTION
    This is a DEMO, not a production signing path. It:
      * creates a short-lived self-signed code-signing certificate in CurrentUser\My;
      * signs a COPY of the setup with signtool;
      * records that Windows sees the signer but does not trust the self-signed root;
      * deletes the private key/PFX and the certificate from CurrentUser\My.

    It never adds anything to Root or TrustedPublisher. The resulting setup is
    expected to fail normal trust validation, which is the point of the demo.

.PARAMETER SetupPath
    Path to the unsigned setup executable.

.PARAMETER OutputDir
    Directory for the signed copy, certificate, verification log and result JSON.

.EXAMPLE
    pwsh -NoProfile -File tools/release/sign_windows_selfsigned_demo.ps1 `
      -SetupPath _work/release-staging/goo-engine-5.2.1-windows-x64-setup.exe `
      -OutputDir _work/signing-demo
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$SetupPath,

    [string]$OutputDir = "_work/signing-demo"
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not (Test-Path -LiteralPath $SetupPath)) {
    throw "setup not found: $SetupPath"
}

$setup = (Resolve-Path -LiteralPath $SetupPath).Path
$outDir = if ([System.IO.Path]::IsPathRooted($OutputDir)) {
    $OutputDir
} else {
    Join-Path $repoRoot $OutputDir
}
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$signtool = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
if (-not (Test-Path -LiteralPath $signtool)) {
    throw "signtool not found: $signtool"
}

$signed = Join-Path $outDir "goo-engine-5.2.1-windows-x64-setup-selfsigned.exe"
$pfx = Join-Path $outDir "GooEngineTestSigning.pfx"
$cer = Join-Path $outDir "GooEngineTestSigning.cer"
$verifyLog = Join-Path $outDir "verify.txt"
$resultJson = Join-Path $outDir "result.json"

$passwordText = [guid]::NewGuid().ToString("N")
$password = ConvertTo-SecureString $passwordText -AsPlainText -Force
$cert = $null

try {
    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject "CN=Goo Engine Test Signing" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -KeyExportPolicy Exportable `
        -NotAfter (Get-Date).AddDays(7) `
        -HashAlgorithm SHA256

    Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $password | Out-Null
    Export-Certificate -Cert $cert -FilePath $cer | Out-Null

    Copy-Item -LiteralPath $setup -Destination $signed -Force
    & $signtool sign /fd SHA256 /f $pfx /p $passwordText $signed | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "signtool sign failed with exit code $LASTEXITCODE"
    }

    # Verify the signature cryptographically. This is expected to exit 1 because
    # the self-signed root is not trusted; capture it as evidence rather than
    # pretending the demo is production-trusted.
    $verifyOutput = & $signtool verify /pa /all /v $signed 2>&1
    $verifyExit = $LASTEXITCODE
    $verifyOutput | Set-Content -LiteralPath $verifyLog -Encoding utf8

    $auth = Get-AuthenticodeSignature -FilePath $signed
    $sha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $signed).Hash.ToLowerInvariant()
    $unsignedSha256 = (Get-FileHash -Algorithm SHA256 -LiteralPath $setup).Hash.ToLowerInvariant()

    $result = [ordered]@{
        schema             = 1
        kind               = "self-signed-demo"
        production_trusted = $false
        timestamped        = $false
        unsigned_setup     = $setup
        unsigned_sha256    = $unsignedSha256
        signed_setup       = $signed
        signed_sha256      = $sha256
        signed_size        = (Get-Item -LiteralPath $signed).Length
        certificate_subject = $cert.Subject
        certificate_sha1   = $cert.Thumbprint
        certificate_not_after = $cert.NotAfter.ToString("o")
        signtool_verify_exit = $verifyExit
        authenticode_status = $auth.Status.ToString()
        authenticode_message = $auth.StatusMessage
        signer_subject     = if ($auth.SignerCertificate) { $auth.SignerCertificate.Subject } else { $null }
        notes              = "Self-signed demo only; root is not trusted. If verify exit is 1 and message mentions an untrusted root, that is expected."
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $resultJson -Encoding utf8

    Write-Output "SIGNED=$signed"
    Write-Output "SIGNED_SHA256=$sha256"
    Write-Output "SIGNED_SIZE=$($result.signed_size)"
    Write-Output "CERT_SHA1=$($cert.Thumbprint)"
    Write-Output "SIGNTOOL_VERIFY_EXIT=$verifyExit"
    Write-Output "AUTHENTICODE_STATUS=$($auth.Status)"
    Write-Output "RESULT_JSON=$resultJson"
    Write-Output "PRODUCTION_TRUSTED=no"
}
finally {
    if ($cert) {
        Remove-Item -LiteralPath "Cert:\CurrentUser\My\$($cert.Thumbprint)" -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -LiteralPath $pfx -Force -ErrorAction SilentlyContinue
}
