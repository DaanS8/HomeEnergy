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
if ($Version -ne $projectVersion) { throw "Version $Version does not match pyproject.toml ($projectVersion). Bump the project version first." }
$releaseDir = Join-Path $projectRoot "dist/home-energy-$Version"
if (Test-Path -LiteralPath $releaseDir) { throw "Release already exists: $releaseDir. Choose another version or archive the existing release." }
$releaseNotes = Join-Path $PSScriptRoot 'RELEASE_NOTES.md'
if (-not (Test-Path -LiteralPath $releaseNotes -PathType Leaf)) { throw "Missing release notes: $releaseNotes" }

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
    $stackYaml = [IO.File]::ReadAllText((Join-Path $PSScriptRoot 'compose.portainer.yaml'))
    $stackYaml = [regex]::Replace($stackYaml, '(?m)^(\s*image:\s*)ghcr\.io/daans8/home-energy:[^\s#]+', {
        param($match)
        $match.Groups[1].Value + "ghcr.io/daans8/home-energy:$Version"
    })
    [IO.File]::WriteAllText((Join-Path $releaseDir 'compose.portainer.yaml'), $stackYaml)
    Copy-Item -LiteralPath (Join-Path $projectRoot 'README.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'RELEASE_NOTES.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'HOME_SERVER_UPDATE.md') -Destination $releaseDir
    Copy-Item -LiteralPath (Join-Path $projectRoot 'docs') -Destination $releaseDir -Recurse
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'n8n') -Destination $releaseDir -Recurse
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
