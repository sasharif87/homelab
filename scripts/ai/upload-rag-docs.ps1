param(
    [Parameter(Mandatory=$true)]
    [string]$ApiKey,
    [string]$BaseUrl = "http://<server-ip>:3000"
)

$headers = @{
    "Authorization" = "Bearer $ApiKey"
    "Accept"        = "application/json"
}

$docs = @(
    # Homelab
    "e:\coding\homelab\Docs\01_Master_Reference.md",
    "e:\coding\homelab\Docs\03_Hardware_Procurement.md",
    "e:\coding\homelab\Docs\04_Compute_AI_Cluster.md",
    "e:\coding\homelab\Docs\Ref docs\06_Discord_Alternative_Decision.md",
    "e:\coding\homelab\Docs\Ref docs\07_Trackers_and_Indexers.md",
    "e:\coding\homelab\Docs\Ref docs\08_Forgejo_Git_Remote_Setup.md",
    "e:\coding\homelab\Docs\infra.md",
    "e:\coding\homelab\Docs\Ref docs\knowledge-archive.md",
    "e:\coding\homelab\Docs\services.md",
    "e:\coding\homelab\Docs\TODO.md",
    # Top-level coding
    "e:\coding\PROJECTS.md",
    "e:\coding\CLAUDE_ENGINEERING_ASSESSMENT.md",
    "e:\coding\PORTFOLIO_ENGINEERING_ASSESSMENT.md",
    # ADHD App
    "e:\coding\ADHD App\docs\ARCHITECTURE.md",
    "e:\coding\ADHD App\docs\quiet-layer-build-todo.md",
    "e:\coding\ADHD App\docs\quiet-layer-data-ingestion.md",
    "e:\coding\ADHD App\docs\quiet-layer-onboarding-v2_5.md",
    "e:\coding\ADHD App\docs\quiet-layer-state-architecture.md",
    "e:\coding\ADHD App\docs\quiet-layer-trust-engine.md",
    # CDA Calculator
    "e:\coding\CDA Calculator\ARCHITECTURE.md",
    "e:\coding\CDA Calculator\ARCHITECTURE_REVIEW.md",
    "e:\coding\CDA Calculator\README.md",
    "e:\coding\CDA Calculator\TODO.md",
    "e:\coding\CDA Calculator\walkthrough-docker.md",
    # Personal Training App
    "e:\coding\Personal training app\ARCHITECTURE_REVIEW.md",
    "e:\coding\Personal training app\README.md",
    "e:\coding\Personal training app\docs\Architecture.md",
    "e:\coding\Personal training app\docs\Coaching_02_Planning.md",
    "e:\coding\Personal training app\docs\Coaching_03_Training_Logic.md",
    "e:\coding\Personal training app\docs\Coaching_04_Data_Integrations.md",
    "e:\coding\Personal training app\docs\Coaching_05_Nutrition.md",
    "e:\coding\Personal training app\docs\Coaching_06_Code_Scratchpad.md",
    "e:\coding\Personal training app\fitness_pipeline.md",
    # SME App Dev
    "e:\coding\SME App Dev\ARCHITECTURE_REVIEW.md",
    "e:\coding\SME App Dev\CONTRIBUTING.md",
    "e:\coding\SME App Dev\README.md",
    "e:\coding\SME App Dev\.agents\rules\IME_Dev_Rules_v4_0.md",
    "e:\coding\SME App Dev\docs\IME_Architecture_v6_0.md",
    "e:\coding\SME App Dev\docs\IME_Formal_SME_Algorithm_Spec_v1_0.md",
    "e:\coding\SME App Dev\docs\IME_Onboarding_Path_Spec_v1_0.md",
    "e:\coding\SME App Dev\docs\IME_Proactive_Surfacing_Spec_v1_0.md",
    "e:\coding\SME App Dev\docs\TODO.md",
    # homeForge
    "e:\coding\homeForge\HomeForge_Design_Outline.md",
    "e:\coding\homeForge\HomeForge_Project_Purpose.md",
    "e:\coding\homeForge\docs\ARCHITECTURE.md"
)

# 1. Create knowledge collection
Write-Host "Creating knowledge collection..."
$body = @{ name = "Coding Projects"; description = "All homelab and project documentation: homelab infra, ADHD App, CDA Calculator, Personal Training App, SME App Dev, homeForge." } | ConvertTo-Json
$collection = Invoke-RestMethod -Uri "$BaseUrl/api/v1/knowledge/create" -Method POST -Headers $headers -Body $body -ContentType "application/json"
$collectionId = $collection.id
Write-Host "Collection created: $collectionId"

# 2. Upload each file and add to collection
$ok = 0; $fail = 0
foreach ($path in $docs) {
    if (-not (Test-Path $path)) { Write-Warning "NOT FOUND: $path"; $fail++; continue }

    $fileName = Split-Path $path -Leaf
    try {
        # Upload file
        $fileBytes = [System.IO.File]::ReadAllBytes($path)
        $boundary  = [System.Guid]::NewGuid().ToString()
        $LF        = "`r`n"
        $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes(
            "--$boundary$LF" +
            "Content-Disposition: form-data; name=`"file`"; filename=`"$fileName`"$LF" +
            "Content-Type: text/markdown$LF$LF"
        ) + $fileBytes + [System.Text.Encoding]::UTF8.GetBytes("$LF--$boundary--$LF")

        $uploadResp = Invoke-RestMethod -Uri "$BaseUrl/api/v1/files/" -Method POST `
            -Headers $headers `
            -Body $bodyBytes `
            -ContentType "multipart/form-data; boundary=$boundary"

        $fileId = $uploadResp.id

        # Add to collection
        $addBody = @{ file_id = $fileId } | ConvertTo-Json
        Invoke-RestMethod -Uri "$BaseUrl/api/v1/knowledge/$collectionId/file/add" -Method POST `
            -Headers $headers -Body $addBody -ContentType "application/json" | Out-Null

        Write-Host "  [OK] $fileName"
        $ok++
    } catch {
        Write-Warning "  [FAIL] $fileName — $($_.Exception.Message)"
        $fail++
    }
}

Write-Host ""
Write-Host "Done: $ok uploaded, $fail failed"
Write-Host "Knowledge collection ID: $collectionId"
Write-Host "Open WebUI -> Workspace -> Knowledge -> 'Coding Projects' to verify"
