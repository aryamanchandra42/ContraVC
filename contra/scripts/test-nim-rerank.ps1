# Smoke test local Nemotron reranker (matches build.nvidia.com curl example).

$body = @{
    model    = "nvidia/llama-nemotron-rerank-vl-1b-v2"
    query    = @{ text = "which way did the traveler go?" }
    passages = @(
        @{ text = "two roads diverged in a yellow wood, and sorry i could not travel both and be one traveler, long i stood and looked down one as far as i could to where it bent in the undergrowth;" }
        @{ text = "then took the other, as just as fair, and having perhaps the better claim because it was grassy and wanted wear, though as for that the passing there had worn them really about the same," }
        @{ text = "and both that morning equally lay in leaves no step had trodden black. oh, i marked the first for another day! yet knowing how way leads on to way i doubted if i should ever come back." }
        @{ text = "i shall be telling this with a sigh somewhere ages and ages hense: two roads diverged in a wood, and i, i took the one less traveled by, and that has made all the difference." }
    )
    truncate = "END"
} | ConvertTo-Json -Depth 5

$hostPort = if ($env:NIM_RERANK_HOST_PORT) { $env:NIM_RERANK_HOST_PORT } else { "8001" }
Invoke-RestMethod -Uri "http://localhost:${hostPort}/v1/ranking" -Method Post -ContentType "application/json" -Body $body
