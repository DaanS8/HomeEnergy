param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Version = '0.2.0'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$sourceImage = "home-energy:$Version"
$registryImage = "ghcr.io/daans8/home-energy:$Version"

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker $($Arguments[0]) failed. For authentication errors, run: docker login ghcr.io -u DaanS8" }
}

# Publish the built and tested local image; this script does not rebuild it.
Invoke-Docker -Arguments @('image', 'inspect', $sourceImage, '--format', '{{.Id}}')
Invoke-Docker -Arguments @('tag', $sourceImage, $registryImage)
Invoke-Docker -Arguments @('push', $registryImage)

# Confirm the registry can serve the image, and retain its published digest.
Invoke-Docker -Arguments @('pull', $registryImage)
$published = & docker image inspect $registryImage
if ($LASTEXITCODE -ne 0) { throw 'Published image inspection failed.' }
$releaseDir = Join-Path $projectRoot "dist/home-energy-$Version"
if (Test-Path -LiteralPath $releaseDir -PathType Container) {
    [IO.File]::WriteAllText((Join-Path $releaseDir 'registry.json'), ($published -join "`n"))
    $checksums = Get-ChildItem -LiteralPath $releaseDir -File -Recurse | Where-Object { $_.Name -ne 'SHA256SUMS' } | Sort-Object FullName | ForEach-Object {
        $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
        $relativePath = $_.FullName.Substring($releaseDir.Length + 1).Replace('\', '/')
        "$($hash.Hash.ToLower())  $relativePath"
    }
    [IO.File]::WriteAllLines((Join-Path $releaseDir 'SHA256SUMS'), $checksums)
}
Write-Output "Published and verified: $registryImage"
