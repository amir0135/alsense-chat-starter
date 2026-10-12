param location string
param aiLocation string
param environmentName string
param modelName string
param modelVersion string
param modelDeployment string
param modelCapacity int
@secure()
param demoAccessCode string
param imageTag string
param tags object

var token = uniqueString(resourceGroup().id)
var acrPullRole = '7f951dda-4ed3-4680-a7ca-43fe172d538d'
var secretUserRole = '4633458b-17de-408a-b874-0445c86b69e6'
var foundryUserRole = '53ca6127-db72-4b80-b1b0-d745d6d5456d'
var openAiUserRole = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: 'id-${environmentName}'
  location: location
  tags: tags
}

resource registry 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: 'acr${token}'
  location: location
  tags: tags
  sku: { name: 'Basic' }
  properties: {
    adminUserEnabled: false
  }
}

resource pull 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: registry
  name: guid(registry.id, identity.id, acrPullRole)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', acrPullRole)
  }
}

resource logs 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-${environmentName}'
  location: location
  tags: tags
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
    workspaceCapping: { dailyQuotaGb: 1 }
  }
}

resource insights 'Microsoft.Insights/components@2020-02-02' = {
  name: 'appi-${environmentName}'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logs.id
  }
}

resource vault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: 'kv${token}'
  location: location
  tags: tags
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    enablePurgeProtection: true
    publicNetworkAccess: 'Disabled'
  }
}

resource code 'Microsoft.KeyVault/vaults/secrets@2023-07-01' = {
  parent: vault
  name: 'demo-access-code'
  properties: { value: demoAccessCode }
}

resource secretReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: vault
  name: guid(vault.id, identity.id, secretUserRole)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', secretUserRole)
  }
}

resource account 'Microsoft.CognitiveServices/accounts@2025-06-01' = {
  name: 'ai-${environmentName}-${token}'
  location: aiLocation
  tags: tags
  kind: 'AIServices'
  sku: { name: 'S0' }
  identity: { type: 'SystemAssigned' }
  properties: {
    allowProjectManagement: true
    customSubDomainName: 'ai-${environmentName}-${token}'
    disableLocalAuth: true
    publicNetworkAccess: 'Enabled'
  }
}

resource deployment 'Microsoft.CognitiveServices/accounts/deployments@2025-06-01' = {
  parent: account
  name: modelDeployment
  sku: { name: 'DataZoneStandard', capacity: modelCapacity }
  properties: {
    model: { format: 'OpenAI', name: modelName, version: modelVersion }
    versionUpgradeOption: 'NoAutoUpgrade'
  }
}

resource project 'Microsoft.CognitiveServices/accounts/projects@2025-06-01' = {
  parent: account
  name: 'alsense'
  location: aiLocation
  tags: tags
  identity: { type: 'SystemAssigned' }
  properties: {
    displayName: 'Alsense synthetic demo'
    description: 'Backend-owned tools against synthetic operations data.'
  }
  // Foundry serializes account writes; project and model creation must not race.
  dependsOn: [deployment]
}

resource modelUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: account
  name: guid(account.id, identity.id, openAiUserRole)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', openAiUserRole)
  }
}

resource projectUser 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: project
  name: guid(project.id, identity.id, foundryUserRole)
  properties: {
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', foundryUserRole)
  }
}

var endpoint = 'https://${account.properties.customSubDomainName}.services.ai.azure.com/api/projects/${project.name}'

resource environment 'Microsoft.App/managedEnvironments@2025-01-01' = {
  name: 'cae-${environmentName}'
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logs.properties.customerId
        sharedKey: logs.listKeys().primarySharedKey
      }
    }
    workloadProfiles: [{ name: 'Consumption', workloadProfileType: 'Consumption' }]
  }
}

resource app 'Microsoft.App/containerApps@2025-01-01' = if (!empty(imageTag)) {
  name: 'ca-${environmentName}'
  location: location
  tags: tags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: { '${identity.id}': {} }
  }
  properties: {
    managedEnvironmentId: environment.id
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: { external: true, targetPort: 8000, transport: 'http', allowInsecure: false }
      registries: [{ server: registry.properties.loginServer, identity: identity.id }]
      secrets: [{
        name: 'demo-access-code'
        value: demoAccessCode
      }]
    }
    template: {
      containers: [{
        name: 'chat'
        image: '${registry.properties.loginServer}/alsense-chat:${imageTag}'
        resources: { cpu: json('0.5'), memory: '1Gi' }
        env: [
          { name: 'CLOUD_DEMO', value: 'true' }
          { name: 'DEMO_LOGIN', value: 'true' }
          { name: 'DEMO_ACCESS_CODE', secretRef: 'demo-access-code' }
          { name: 'CHAT_ENABLED', value: 'true' }
          { name: 'AGENT_MODE', value: 'foundry' }
          { name: 'SESSION_DB', value: '/tmp/sessions.sqlite3' }
          { name: 'FOUNDRY_PROJECT_ENDPOINT', value: endpoint }
          { name: 'MODEL_DEPLOYMENT', value: modelDeployment }
          { name: 'AZURE_CLIENT_ID', value: identity.properties.clientId }
          { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: insights.properties.ConnectionString }
          { name: 'OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT', value: 'false' }
        ]
        probes: [
          { type: 'Startup', httpGet: { path: '/api/health', port: 8000 }, periodSeconds: 5, failureThreshold: 36 }
          { type: 'Readiness', httpGet: { path: '/api/health', port: 8000 }, periodSeconds: 10 }
          { type: 'Liveness', httpGet: { path: '/api/health', port: 8000 }, periodSeconds: 30 }
        ]
      }]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
  dependsOn: [pull, projectUser, modelUser]
}

output registryName string = registry.name
output projectEndpoint string = endpoint
output appUrl string = empty(imageTag) ? '' : 'https://${app!.properties.configuration.ingress.fqdn}'
