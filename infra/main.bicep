targetScope = 'subscription'

@minLength(3)
@maxLength(20)
param environmentName string = 'alsense-demo'

param location string = 'northeurope'
param aiLocation string = 'swedencentral'
param modelName string = 'gpt-5-mini'
param modelVersion string = '2025-08-07'
param modelDeployment string = 'gpt-5-mini'
@minValue(1)
param modelCapacity int = 10

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
    demoAccessCode: demoAccessCode
    imageTag: imageTag
    tags: group.tags
  }
}

output resourceGroupName string = group.name
output registryName string = resources.outputs.registryName
output projectEndpoint string = resources.outputs.projectEndpoint
output appUrl string = resources.outputs.appUrl
