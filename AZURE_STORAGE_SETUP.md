# Azure Blob Storage Setup for Skyvern

This guide explains how to configure Skyvern to use Azure Blob Storage alongside or instead of AWS S3 storage.

## Prerequisites

- An Azure account with an active subscription
- Azure Storage Account created
- Access keys or connection string for your Storage Account

## Configuration

Skyvern supports three storage types: `local`, `s3`, and `azure`. You can switch between them by setting the `SKYVERN_STORAGE_TYPE` environment variable.

### Environment Variables

Add the following environment variables to your `.env` file or set them in your environment:

```bash
# Set storage type to Azure
SKYVERN_STORAGE_TYPE=azure

# Azure Storage Account Configuration (choose one method)
# Method 1: Using Connection String (recommended)
AZURE_STORAGE_CONNECTION_STRING="DefaultEndpointsProtocol=https;AccountName=your_account_name;AccountKey=your_account_key;EndpointSuffix=core.windows.net"

# Method 2: Using Account Name and Key
AZURE_STORAGE_ACCOUNT_NAME=your_storage_account_name
AZURE_STORAGE_ACCOUNT_KEY=your_storage_account_key

# Azure Container Names (optional - defaults shown)
AZURE_STORAGE_CONTAINER_ARTIFACTS=skyvern-artifacts
AZURE_STORAGE_CONTAINER_SCREENSHOTS=skyvern-screenshots
AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS=skyvern-browser-sessions
AZURE_STORAGE_CONTAINER_UPLOADS=skyvern-uploads
```

### Docker Compose Configuration

If you're using Docker Compose, add these environment variables to your `docker-compose.yml`:

```yaml
services:
  skyvern:
    environment:
      - SKYVERN_STORAGE_TYPE=azure
      - AZURE_STORAGE_CONNECTION_STRING=${AZURE_STORAGE_CONNECTION_STRING}
      # OR use account name and key
      # - AZURE_STORAGE_ACCOUNT_NAME=${AZURE_STORAGE_ACCOUNT_NAME}
      # - AZURE_STORAGE_ACCOUNT_KEY=${AZURE_STORAGE_ACCOUNT_KEY}
      - AZURE_STORAGE_CONTAINER_ARTIFACTS=skyvern-artifacts
      - AZURE_STORAGE_CONTAINER_SCREENSHOTS=skyvern-screenshots
      - AZURE_STORAGE_CONTAINER_BROWSER_SESSIONS=skyvern-browser-sessions
      - AZURE_STORAGE_CONTAINER_UPLOADS=skyvern-uploads
```

## Getting Your Azure Storage Credentials

### Method 1: Azure Portal

1. Log in to the [Azure Portal](https://portal.azure.com)
2. Navigate to your Storage Account
3. Under "Security + networking", click on "Access keys"
4. Copy either key1 or key2
5. For connection string, copy the "Connection string" value

### Method 2: Azure CLI

```bash
# Get storage account key
az storage account keys list --account-name your_storage_account_name --resource-group your_resource_group

# Get connection string
az storage account show-connection-string --name your_storage_account_name --resource-group your_resource_group
```

## Creating Required Containers

Skyvern will attempt to use the following containers. Make sure they exist in your storage account:

```bash
# Using Azure CLI
az storage container create --name skyvern-artifacts --account-name your_storage_account_name
az storage container create --name skyvern-screenshots --account-name your_storage_account_name
az storage container create --name skyvern-browser-sessions --account-name your_storage_account_name
az storage container create --name skyvern-uploads --account-name your_storage_account_name
```

## Features

When using Azure Blob Storage, Skyvern supports:

- **Artifact Storage**: All artifacts (screenshots, HAR files, logs) are stored in Azure
- **File Downloads**: Downloaded files are automatically uploaded to Azure Blob Storage
- **Browser Sessions**: Browser session data is zipped and stored in Azure
- **Presigned URLs**: Temporary access URLs are generated using SAS tokens
- **Blob Tiers**: Support for Hot, Cool, Cold, and Archive tiers (defaults to Hot)
- **Metadata & Tags**: Support for custom metadata and tags on blobs

## Switching Between Storage Providers

You can easily switch between storage providers by changing the `SKYVERN_STORAGE_TYPE` environment variable:

- `SKYVERN_STORAGE_TYPE=local` - Use local file system
- `SKYVERN_STORAGE_TYPE=s3` - Use AWS S3
- `SKYVERN_STORAGE_TYPE=azure` - Use Azure Blob Storage

## Advanced Configuration

### Custom Blob Tiers

You can customize the blob tier by extending the `AzureBlobStorage` class:

```python
from skyvern.forge.sdk.api.azure import AzureBlobTier
from skyvern.forge.sdk.artifact.storage.azure_blob import AzureBlobStorage

class CustomAzureBlobStorage(AzureBlobStorage):
    async def _get_blob_tier_for_org(self, organization_id: str) -> AzureBlobTier:
        # Implement custom logic to determine blob tier
        # For example, use Cool tier for specific organizations
        if organization_id in ["org1", "org2"]:
            return AzureBlobTier.COOL
        return AzureBlobTier.HOT
```

### Custom Tags

You can add custom tags to your blobs by extending the storage class:

```python
class CustomAzureBlobStorage(AzureBlobStorage):
    async def _get_tags_for_org(self, organization_id: str) -> dict[str, str]:
        return {
            "environment": "production",
            "organization": organization_id,
            "application": "skyvern"
        }
```

## Troubleshooting

### Common Issues

1. **Authentication Failed**: Ensure your connection string or account key is correct
2. **Container Not Found**: Make sure all required containers are created
3. **Access Denied**: Check that your account has the necessary permissions
4. **Network Issues**: Ensure your firewall rules allow access to Azure Storage

### Debugging

Enable debug logging to see detailed Azure Storage operations:

```bash
DEBUG_MODE=true
```

## Security Best Practices

1. **Use Managed Identities**: When running in Azure, use managed identities instead of keys
2. **Rotate Keys Regularly**: Rotate your storage account keys periodically
3. **Use SAS Tokens**: For temporary access, use SAS tokens instead of sharing keys
4. **Enable HTTPS**: Always use HTTPS endpoints (default in connection strings)
5. **Restrict Network Access**: Configure firewall rules to limit access to your storage account

## Performance Considerations

- **Blob Tiers**: Use Hot tier for frequently accessed data, Cool/Cold for archives
- **Parallel Uploads**: Skyvern uses async operations for efficient uploads
- **Regional Deployment**: Deploy Skyvern in the same region as your storage account to reduce latency