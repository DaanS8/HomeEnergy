param(
    [ValidatePattern('^[A-Za-z0-9_.-]+$')]
    [string]$Version = '0.1.0'
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$releaseDir = Join-Path $projectRoot "dist/home-energy-$Version"
if (Test-Path -LiteralPath $releaseDir) { throw "Release already exists: $releaseDir. Choose another version or archive the existing release." }

function Invoke-Docker {
    param([string[]]$Arguments)
    & docker @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed: $($Arguments[0])" }
}

Push-Location $projectRoot
try {
    Invoke-Docker -Arguments @('build', '--platform', 'linux/amd64', '--build-arg', "VERSION=$Version", '-t', "home-energy:$Version", '-t', "ghcr.io/daans8/home-energy:$Version", '.')
    $imageNames = @("home-energy:$Version", "ghcr.io/daans8/home-energy:$Version")
    New-Item -ItemType Directory -Path $releaseDir | Out-Null
    Invoke-Docker -Arguments (@('save', '-o', (Join-Path $releaseDir 'images.tar')) + $imageNames)
    $stackYaml = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'compose.portainer.yaml')).Replace('home-energy:0.1.0', "home-energy:$Version")
    [IO.File]::WriteAllText((Join-Path $releaseDir 'compose.portainer.yaml'), $stackYaml)
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs') -Destination $releaseDir -Recurse
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'n8n-day-ahead.json') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'publish-image.ps1') -Destination $releaseDir
    $images = & docker image inspect @imageNames
    if ($LASTEXITCODE -ne 0) { throw 'Image inspection failed.' }
    [IO.File]::WriteAllText((Join-Path $releaseDir 'images.json'), ($images -join "`n"))
    $checksums = Get-ChildItem -LiteralPath $releaseDir -File -Recurse | Sort-Object FullName | ForEach-Object {
        $hash = Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256
        $relativePath = $_.FullName.Substring($releaseDir.Length + 1).Replace('\', '/')
        "$($hash.Hash.ToLower())  $relativePath"
    }
    [IO.File]::WriteAllLines((Join-Path $releaseDir 'SHA256SUMS'), $checksums)
    Write-Output "Release: $releaseDir"
} finally {
    Pop-Location
}
