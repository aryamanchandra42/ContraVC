# Pull and run llama-nemotron-rerank-vl-1b-v2 locally (Windows / Docker Desktop + GPU).
# Requires NGC_API_KEY in .env or environment (same nvapi key from build.nvidia.com).

$ErrorActionPreference = "Stop"
$contraRoot = Split-Path -Parent $PSScriptRoot
$envFile = Join-Path $contraRoot ".env"

if (-not $env:NGC_API_KEY -and (Test-Path $envFile)) {
    Get-Content $envFile | ForEach-Object {
        if ($_ -match '^\s*NGC_API_KEY=(.+)$') { $env:NGC_API_KEY = $Matches[1].Trim() }
    }
}

if (-not $env:NGC_API_KEY) {
    Write-Error "Set NGC_API_KEY in contra/.env or your shell before running."
}

if (-not $env:LOCAL_NIM_CACHE) {
    $env:LOCAL_NIM_CACHE = Join-Path $env:USERPROFILE ".cache\nim"
}
New-Item -ItemType Directory -Force -Path $env:LOCAL_NIM_CACHE | Out-Null

Write-Host "Logging in to nvcr.io..."
$env:NGC_API_KEY | docker login nvcr.io -u '$oauthtoken' --password-stdin

$hostPort = if ($env:NIM_RERANK_HOST_PORT) { $env:NIM_RERANK_HOST_PORT } else { "8001" }
Write-Host "Starting rerank NIM on http://localhost:${hostPort}/v1/ranking (Contra API uses :8000) ..."
docker run -it --rm `
    --gpus all `
    --shm-size=16GB `
    -e NGC_API_KEY `
    -e HF_HOME=/tmp/huggingface `
    -v "${env:LOCAL_NIM_CACHE}:/opt/nim/.cache" `
    -p "${hostPort}:8000" `
    nvcr.io/nim/nvidia/llama-nemotron-rerank-vl-1b-v2:latest
