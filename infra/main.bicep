targetScope = 'subscription'

@minLength(3)
@maxLength(20)
param environmentName string = 'alsense-demo'

param location string = 'northeurope'
param aiLocation string = 'swedencentral'
param modelName string = 'gpt-6.1-terra'
param modelVersion string = '2026-10-26'
param modelDeployment string = 'gpt-6.1-terra'
@description('Thousands of tokens per minute. Each model call reserves its prompt plus MODEL_MAX_OUTPUT_TOKENS.')
@minValue(1)
param modelCapacity int = 250

// The model prices for the app's estimated cost. Strings, since Bicep has no decimal type; empty means not set.
@description('US dollars per million input tokens.')
param modelPriceInput string = ''
@description('US dollars per million cached input tokens.')
param modelPriceCachedInput string = ''
@description('US dollars per million output tokens, reasoning included.')
param modelPriceOutput string = ''

@secure()
@minLength(32)
param demoAccessCode string

@description('Empty during the infrastructure bootstrap; deploy the app after building its image.')
param imageTag string = ''

resource group 'Microsoft.Resources/resourceGroups@2024-03-01' = {
  name: 'rg-${environmentName}'
  location: location
  tags: {
    application: 'alsense-synthetic-starter'
    environment: environmentName
    data: 'synthetic-only'
  }
}

module resources './resources.bicep' = {
  name: 'alsense-resources'
  scope: group
  params: {
    location: location
    aiLocation: aiLocation
    environmentName: environmentName
    modelName: modelName
    modelVersion: modelVersion
    modelDeployment: modelDeployment
    modelCapacity: modelCapacity
    modelPriceInput: modelPriceInput
    modelPriceCachedInput: modelPriceCachedInput
    modelPriceOutput: modelPriceOutput
    demoAccessCode: demoAccessCode
    imageTag: imageTag
    tags: group.tags
  }
}

output resourceGroupName string = group.name
output registryName string = resources.outputs.registryName
output projectEndpoint string = resources.outputs.projectEndpoint
output appUrl string = resources.outputs.appUrl
