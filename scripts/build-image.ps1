param(
    [string]$ImageName = "overleaf-git-bridge",
    [string]$Tag = "latest",
    [string]$Dockerfile = "Dockerfile",
    [switch]$Push
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$imageRef = "${ImageName}:${Tag}"

Write-Host "Building $imageRef from $Dockerfile"
docker build -f (Join-Path $repoRoot $Dockerfile) -t $imageRef $repoRoot

if ($Push) {
    Write-Host "Pushing $imageRef"
    docker push $imageRef
}
