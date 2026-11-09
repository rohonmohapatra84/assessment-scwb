// Grants the app's managed identity permission to call the Foundry / Azure OpenAI resource (keyless auth).
// Deployed at the scope of the resource group that holds the Foundry resource.
targetScope = 'resourceGroup'

param foundryResourceName string
param principalId string

// Cognitive Services OpenAI User: may call model deployments (chat completions); cannot manage the resource or read keys.
var openAiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource foundry 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = {
  name: foundryResourceName
}

resource assignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundry.id, principalId, openAiUserRoleId)
  scope: foundry
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRoleId)
  }
}
