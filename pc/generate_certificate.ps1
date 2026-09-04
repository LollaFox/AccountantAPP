param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

$ErrorActionPreference = "Stop"
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

function Write-DerLength {
    param(
        [Parameter(Mandatory = $true)][System.IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][int]$Length
    )
    if ($Length -lt 128) {
        $Stream.WriteByte([byte]$Length)
        return
    }
    $bytes = [BitConverter]::GetBytes($Length)
    $last = $bytes.Length - 1
    while ($last -gt 0 -and $bytes[$last] -eq 0) { $last-- }
    $Stream.WriteByte([byte](0x80 -bor ($last + 1)))
    for ($index = $last; $index -ge 0; $index--) { $Stream.WriteByte($bytes[$index]) }
}

function Write-DerInteger {
    param(
        [Parameter(Mandatory = $true)][System.IO.Stream]$Stream,
        [Parameter(Mandatory = $true)][byte[]]$Bytes
    )
    $offset = 0
    while ($offset -lt $Bytes.Length - 1 -and $Bytes[$offset] -eq 0) { $offset++ }
    $length = $Bytes.Length - $offset
    $needsLeadingZero = ($Bytes[$offset] -band 0x80) -ne 0
    $Stream.WriteByte(0x02)
    Write-DerLength -Stream $Stream -Length ($length + [int]$needsLeadingZero)
    if ($needsLeadingZero) { $Stream.WriteByte(0) }
    $Stream.Write($Bytes, $offset, $length)
}

function Export-RsaPrivateKeyDer {
    param([Parameter(Mandatory = $true)][System.Security.Cryptography.RSA]$Rsa)
    $parameters = $Rsa.ExportParameters($true)
    $content = [System.IO.MemoryStream]::new()
    try {
        $content.Write([byte[]](0x02, 0x01, 0x00), 0, 3)
        foreach ($value in @(
            $parameters.Modulus, $parameters.Exponent, $parameters.D, $parameters.P,
            $parameters.Q, $parameters.DP, $parameters.DQ, $parameters.InverseQ
        )) {
            Write-DerInteger -Stream $content -Bytes $value
        }
        $body = $content.ToArray()
        $result = [System.IO.MemoryStream]::new()
        try {
            $result.WriteByte(0x30)
            Write-DerLength -Stream $result -Length $body.Length
            $result.Write($body, 0, $body.Length)
            return [byte[]]$result.ToArray()
        }
        finally {
            $result.Dispose()
        }
    }
    finally {
        $content.Dispose()
    }
}

function ConvertTo-Pem {
    param(
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][byte[]]$Data
    )
    $base64 = [Convert]::ToBase64String($Data, [Base64FormattingOptions]::InsertLineBreaks)
    return "-----BEGIN $Label-----`r`n$base64`r`n-----END $Label-----`r`n"
}

$rsa = [System.Security.Cryptography.RSA]::Create(3072)
try {
    $distinguishedName = [System.Security.Cryptography.X509Certificates.X500DistinguishedName]::new("CN=Receipt Sync")
    $request = [System.Security.Cryptography.X509Certificates.CertificateRequest]::new(
        $distinguishedName,
        $rsa,
        [System.Security.Cryptography.HashAlgorithmName]::SHA256,
        [System.Security.Cryptography.RSASignaturePadding]::Pkcs1
    )
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true)
    )
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new(
            [System.Security.Cryptography.X509Certificates.X509KeyUsageFlags]::DigitalSignature,
            $true
        )
    )
    $enhancedUsage = [System.Security.Cryptography.OidCollection]::new()
    [void]$enhancedUsage.Add([System.Security.Cryptography.Oid]::new("1.3.6.1.5.5.7.3.1"))
    $request.CertificateExtensions.Add(
        [System.Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($enhancedUsage, $true)
    )
    $san = [System.Security.Cryptography.X509Certificates.SubjectAlternativeNameBuilder]::new()
    $san.AddDnsName("localhost")
    $san.AddDnsName($env:COMPUTERNAME)
    $san.AddIpAddress([System.Net.IPAddress]::Loopback)
    $request.CertificateExtensions.Add($san.Build())
    $certificate = $request.CreateSelfSigned([DateTimeOffset]::UtcNow.AddDays(-1), [DateTimeOffset]::UtcNow.AddYears(5))
    try {
        $certificatePath = Join-Path $OutputDirectory "receipt-sync-cert.pem"
        $keyPath = Join-Path $OutputDirectory "receipt-sync-key.pem"
        $certificatePem = ConvertTo-Pem -Label "CERTIFICATE" -Data $certificate.RawData
        $privateKeyDer = [byte[]](Export-RsaPrivateKeyDer -Rsa $rsa)
        $privateKeyPem = ConvertTo-Pem -Label "RSA PRIVATE KEY" -Data $privateKeyDer
        [System.IO.File]::WriteAllText($certificatePath, $certificatePem, [System.Text.Encoding]::ASCII)
        [System.IO.File]::WriteAllText($keyPath, $privateKeyPem, [System.Text.Encoding]::ASCII)
        $sha256 = [System.Security.Cryptography.SHA256]::Create()
        try {
            $fingerprint = [BitConverter]::ToString($sha256.ComputeHash($certificate.RawData)).Replace("-", "")
        }
        finally {
            $sha256.Dispose()
        }
        [System.IO.File]::WriteAllText((Join-Path $OutputDirectory "receipt-sync-sha256.txt"), $fingerprint, [System.Text.Encoding]::ASCII)
        Write-Host "HTTPS certificate created. SHA-256: $fingerprint"
    }
    finally {
        $certificate.Dispose()
    }
}
finally {
    $rsa.Dispose()
}
