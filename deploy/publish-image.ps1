param(
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$projectFile = [IO.File]::ReadAllText((Join-Path $projectRoot 'pyproject.toml'))
$projectVersion = [regex]::Match($projectFile, '(?m)^version = "(\d+\.\d+\.\d+)"').Groups[1].Value
if (-not $projectVersion) { throw 'Cannot read the project version from pyproject.toml.' }
if (-not $Version) { $Version = $projectVersion }
if ($Version -ne $projectVersion) { throw "Version $Version does not match pyproject.toml ($projectVersion)." }
$sourceImage = "home-energy:$Version"
$registryImage = "ghcr.io/daans8/home-energy:$Version"

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker $($Arguments[0]) failed. For authentication errors, run: docker login ghcr.io -u DaanS8" }
}

# Publish the built and tested local image; this script does not rebuild it.
$localImage = & docker image inspect $sourceImage
if ($LASTEXITCODE -ne 0) { throw "Build and smoke-test $sourceImage before publishing." }
$localMetadata = ($localImage -join "`n" | ConvertFrom-Json)[0]
if ($localMetadata.Config.Labels.'org.opencontainers.image.version' -ne $Version) {
    throw 'The local image version label does not match the release version.'
}
# This package is public. Fail closed if its tags cannot be read.
$registryToken = (Invoke-RestMethod -Uri 'https://ghcr.io/token?scope=repository:daans8/home-energy:pull&service=ghcr.io').token
$registryHeaders = @{ Authorization = "Bearer $registryToken" }
$registryTags = Invoke-RestMethod -Uri 'https://ghcr.io/v2/daans8/home-energy/tags/list' -Headers $registryHeaders
if ($registryTags.tags -contains $Version) { throw "Tag $registryImage is already published. Choose a new version; existing releases are immutable." }
Invoke-Docker -Arguments @('tag', $sourceImage, $registryImage)
Invoke-Docker -Arguments @('push', $registryImage)

# Confirm the registry can serve the image, and retain its published digest.
Invoke-Docker -Arguments @('pull', $registryImage)
$published = & docker image inspect $registryImage
if ($LASTEXITCODE -ne 0) { throw 'Published image inspection failed.' }
$publishedMetadata = ($published -join "`n" | ConvertFrom-Json)[0]
if ($publishedMetadata.Id -ne $localMetadata.Id) { throw 'The pulled registry image differs from the local release image.' }
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
