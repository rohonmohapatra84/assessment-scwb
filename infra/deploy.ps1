<#
.SYNOPSIS
  Deploys the agentic SDLC API to Azure Container Apps, wired to an existing Azure AI Foundry model (keyless).

.EXAMPLE
  az login
  ./infra/deploy.ps1 -ResourceGroup rg-agentic -FoundryResource my-foundry-res `
      -FoundryEndpoint https://my-foundry-res.openai.azure.com -FoundryDeployment gpt-4.1

  Prerequisites: Azure CLI (az), logged in; Contributor + User Access Administrator (or Owner) on the target
  resource group AND on the Foundry resource (the script creates a role assignment there).
  The script never prints or stores a model key: the app authenticates to Foundry with its managed identity.
#>
param(
  [Parameter(Mandatory)] [string] $ResourceGroup,
  [Parameter(Mandatory)] [string] $FoundryResource,
  [Parameter(Mandatory)] [string] $FoundryEndpoint,
  [Parameter(Mandatory)] [string] $FoundryDeployment,
  [string] $FoundryResourceGroup = $ResourceGroup,
  [string] $Location = "eastus2",
  [string] $NamePrefix = "agentic",
  [string] $ApiToken = ""
)
$ErrorActionPreference = "Stop"
function Step($m) { Write-Host "`n==> $m" -ForegroundColor Cyan }

if (-not (Get-Command az -ErrorAction SilentlyContinue)) { throw "Azure CLI not found. Install: https://aka.ms/installazurecliwindows" }
az account show --query name -o tsv | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Not logged in. Run: az login" }
az extension add --name containerapp --upgrade --only-show-errors | Out-Null

$root = Split-Path -Parent $PSScriptRoot
if (-not $ApiToken) {
  $bytes = New-Object byte[] 32
  [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
  $ApiToken = [Convert]::ToBase64String($bytes).Replace('+','-').Replace('/','_').TrimEnd('=')
  $generated = $true
}

Step "Validating the Bicep before touching Azure"
az bicep build --file "$root/infra/main.bicep" --stdout | Out-Null

Step "Resource group $ResourceGroup ($Location)"
az group create -n $ResourceGroup -l $Location --only-show-errors | Out-Null

Step "Infrastructure (ACR, Log Analytics, Container Apps env, app + RBAC to Foundry)"
$out = az deployment group create -g $ResourceGroup -f "$root/infra/main.bicep" `
  -p namePrefix=$NamePrefix foundryResourceName=$FoundryResource foundryResourceGroup=$FoundryResourceGroup `
     foundryEndpoint=$FoundryEndpoint foundryDeployment=$FoundryDeployment apiToken=$ApiToken `
  --query properties.outputs -o json | ConvertFrom-Json
$acr = $out.acrName.value; $app = $out.appName.value; $url = $out.appUrl.value

Step "Building the image in ACR ($acr)  - no local Docker needed"
az acr build -r $acr -t "agentic-sdlc:latest" "$root" --only-show-errors

Step "Switching the app to the real image (port 8080)"
az deployment group create -g $ResourceGroup -f "$root/infra/main.bicep" `
  -p namePrefix=$NamePrefix foundryResourceName=$FoundryResource foundryResourceGroup=$FoundryResourceGroup `
     foundryEndpoint=$FoundryEndpoint foundryDeployment=$FoundryDeployment apiToken=$ApiToken `
     image="$acr.azurecr.io/agentic-sdlc:latest" targetPort=8080 pullFromAcr=true --only-show-errors | Out-Null

Step "Waiting for /healthz"
$ok = $false
for ($i = 0; $i -lt 40 -and -not $ok; $i++) {
  try { $h = Invoke-RestMethod "$url/healthz" -TimeoutSec 5; $ok = ($h.status -eq "ok") } catch { Start-Sleep 6 }
}
if (-not $ok) { throw "App did not become healthy. Check: az containerapp logs show -g $ResourceGroup -n $app --follow" }

Write-Host "`nDeployed.  URL: $url" -ForegroundColor Green
Write-Host "LLM wiring reported by the app: provider=$($h.llm.provider) deployment=$($h.llm.deployment)"
if ($generated) {
  Write-Host "`nAPI token (shown once - save it now):`n  $ApiToken" -ForegroundColor Yellow
}
Write-Host "`nNext:  python scripts/cloud_e2e.py --url $url --token <token>"
