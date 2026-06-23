targetScope = 'resourceGroup'

@description('Deployment environment name.')
@allowed([
  'dev'
  'prod'
])
param environmentName string

@description('Azure region for all resources. Defaults to the resource group location.')
param location string = resourceGroup().location

@description('Short lowercase suffix used for globally unique names.')
@minLength(2)
@maxLength(4)
param nameSuffix string

@description('Application name used in default resource names.')
param appName string = 'birdspotter'

@description('Azure Function App name.')
param functionAppName string = 'func-${appName}-${environmentName}'

@description('App Service Plan name.')
param appServicePlanName string = 'plan-${appName}-${environmentName}'

@description('Application Insights resource name.')
param applicationInsightsName string = 'appi-${appName}-${environmentName}'

@description('Key Vault name. Must be globally unique.')
@minLength(3)
@maxLength(24)
param keyVaultName string = 'kv-${appName}-${environmentName}-${nameSuffix}'

@description('Storage account for Function App runtime. Must be lowercase and globally unique.')
@minLength(3)
@maxLength(24)
param functionStorageAccountName string = toLower('rg${appName}${environmentName}${nameSuffix}')

@description('Storage account for BirdSpotter data. Must be lowercase and globally unique.')
@minLength(3)
@maxLength(24)
param dataStorageAccountName string = toLower('st${appName}${environmentName}')

@description('Blob container for observation cache files.')
param observationsContainerName string = 'observations'

@description('Blob container for metadata cache files.')
param metadataContainerName string = 'metadata'

@description('Blob container for Flex Consumption deployment packages.')
param functionDeploymentContainerName string = 'function-releases'

@description('Key Vault secret name for the SOS API key. The secret value is created manually.')
param sosApiKeySecretName string = 'sos-api-key'

@description('Maximum SOS pages fetched per sync run.')
@minValue(1)
param maxPages int = 10

@description('Enable purge protection on Key Vault.')
param enableKeyVaultPurgeProtection bool = environmentName == 'prod'

var storageSkuName = 'Standard_LRS'
var storageEndpointSuffix = environment().suffixes.storage
var functionStorageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${functionStorageAccount.name};AccountKey=${functionStorageAccount.listKeys().keys[0].value};EndpointSuffix=${storageEndpointSuffix}'
var dataStorageConnectionString = 'DefaultEndpointsProtocol=https;AccountName=${dataStorageAccount.name};AccountKey=${dataStorageAccount.listKeys().keys[0].value};EndpointSuffix=${storageEndpointSuffix}'
var keyVaultSecretsUserRoleDefinitionId = '4633458b-17de-408a-b874-0445c86b69e6'

resource functionStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: functionStorageAccountName
  location: location
  sku: {
    name: storageSkuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource functionBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: functionStorageAccount
  name: 'default'
}

resource functionDeploymentContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: functionBlobService
  name: functionDeploymentContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource dataStorageAccount 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: dataStorageAccountName
  location: location
  sku: {
    name: storageSkuName
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource dataBlobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: dataStorageAccount
  name: 'default'
}

resource observationsContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: dataBlobService
  name: observationsContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource metadataContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = {
  parent: dataBlobService
  name: metadataContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: applicationInsightsName
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
  }
}

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: keyVaultName
  location: location
  properties: {
    tenantId: tenant().tenantId
    enableRbacAuthorization: true
    enablePurgeProtection: enableKeyVaultPurgeProtection
    enableSoftDelete: true
    enabledForDeployment: false
    enabledForDiskEncryption: false
    enabledForTemplateDeployment: false
    publicNetworkAccess: 'Enabled'
    sku: {
      family: 'A'
      name: 'standard'
    }
    softDeleteRetentionInDays: 90
  }
}

resource appServicePlan 'Microsoft.Web/serverfarms@2024-04-01' = {
  name: appServicePlanName
  location: location
  kind: 'functionapp'
  sku: {
    name: 'FC1'
    tier: 'FlexConsumption'
  }
  properties: {
    reserved: true
  }
}

resource functionApp 'Microsoft.Web/sites@2024-04-01' = {
  name: functionAppName
  location: location
  kind: 'functionapp,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    siteConfig: {
      ftpsState: 'Disabled'
      minTlsVersion: '1.2'
      appSettings: [
        {
          name: 'AzureWebJobsStorage'
          value: functionStorageConnectionString
        }
        {
          name: 'FUNCTIONS_EXTENSION_VERSION'
          value: '~4'
        }
        {
          name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
          value: appInsights.properties.ConnectionString
        }
        {
          name: 'KEY_VAULT_URL'
          value: keyVault.properties.vaultUri
        }
        {
          name: 'SOS_API_KEY_SECRET_NAME'
          value: sosApiKeySecretName
        }
        {
          name: 'AZURE_STORAGE_CONNECTION_STRING'
          value: dataStorageConnectionString
        }
        {
          name: 'OBSERVATIONS_CONTAINER_NAME'
          value: observationsContainerName
        }
        {
          name: 'METADATA_CONTAINER_NAME'
          value: metadataContainerName
        }
        {
          name: 'MAX_PAGES'
          value: string(maxPages)
        }
      ]
    }
    functionAppConfig: {
      deployment: {
        storage: {
          type: 'blobContainer'
          value: '${functionStorageAccount.properties.primaryEndpoints.blob}${functionDeploymentContainer.name}'
          authentication: {
            type: 'StorageAccountConnectionString'
            storageAccountConnectionStringName: 'AzureWebJobsStorage'
          }
        }
      }
      runtime: {
        name: 'python'
        version: '3.13'
      }
      scaleAndConcurrency: {
        maximumInstanceCount: 100
        instanceMemoryMB: 2048
      }
    }
  }
}

resource keyVaultSecretsUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(keyVault.id, functionApp.id, keyVaultSecretsUserRoleDefinitionId)
  scope: keyVault
  properties: {
    principalId: functionApp.identity.principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', keyVaultSecretsUserRoleDefinitionId)
  }
}

output functionAppName string = functionApp.name
output functionAppPrincipalId string = functionApp.identity.principalId
output keyVaultName string = keyVault.name
output keyVaultUri string = keyVault.properties.vaultUri
output dataStorageAccountName string = dataStorageAccount.name
output functionStorageAccountName string = functionStorageAccount.name
