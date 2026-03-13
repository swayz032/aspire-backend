$apiKey = $env:N8N_API_KEY
if (-not $apiKey) {
    $repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\\..")).Path
    $mcpPath = Join-Path $repoRoot ".mcp.json"
    if (Test-Path $mcpPath) {
        $mcp = Get-Content $mcpPath -Raw | ConvertFrom-Json
        $apiKey = $mcp.mcpServers."n8n-mcp".env.N8N_API_KEY
    }
}

if (-not $apiKey) {
    throw "Set N8N_API_KEY in the environment or configure .mcp.json before running audit_n8n.ps1"
}

$headers = @{"X-N8N-API-KEY" = $apiKey}

$response = Invoke-RestMethod "http://localhost:5678/api/v1/workflows?limit=50" -Headers $headers
$workflows = $response.data

Write-Host "Total workflows: $($workflows.Count)`n"

foreach ($wf in $workflows) {
    $nodes = $wf.nodes
    $conns = $wf.connections

    # Build connection graph
    $targets = @{}
    $sources = @{}

    $connProps = $conns | Get-Member -MemberType NoteProperty
    foreach ($prop in $connProps) {
        $srcName = $prop.Name
        $sources[$srcName] = $true
        $outputs = $conns.$srcName
        $outputProps = $outputs | Get-Member -MemberType NoteProperty
        foreach ($outProp in $outputProps) {
            $groups = $outputs.($outProp.Name)
            foreach ($group in $groups) {
                foreach ($conn in $group) {
                    $targets[$conn.node] = $true
                }
            }
        }
    }

    $triggerTypes = @("scheduleTrigger", "webhook", "errorTrigger", "manualTrigger")
    $orphans = @()

    foreach ($n in $nodes) {
        $name = $n.name
        $ntype = $n.type.Split(".")[-1]
        $isTrigger = $triggerTypes -contains $ntype -or $ntype -match "Trigger|trigger"
        $isSource = $sources.ContainsKey($name)
        $isTarget = $targets.ContainsKey($name)

        if (-not $isTrigger -and -not $isTarget) {
            $orphans += "$name [NO INPUT]"
        }
        elseif (-not $isTrigger -and -not $isSource -and $ntype -in @("httpRequest", "code", "if", "switch")) {
            $orphans += "$name [DEAD END]"
        }
    }

    $status = if ($orphans.Count -eq 0) { "OK" } else { "BROKEN: " + ($orphans -join ", ") }
    $shortName = $wf.name
    if ($shortName.Length -gt 55) { $shortName = $shortName.Substring(0, 55) }
    Write-Host ("  {0,-20} {1,-55} {2}" -f $wf.id, $shortName, $status)
}
