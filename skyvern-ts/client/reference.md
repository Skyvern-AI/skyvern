# Reference
<details><summary><code>client.<a href="/src/Client.ts">runSdkAction</a>({ ...params }) -> Skyvern.RunSdkActionResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Execute a single SDK action with the specified parameters
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.runSdkAction({
    url: "url",
    action: {
        type: "ai_act"
    }
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**request:** `Skyvern.RunSdkActionRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `SkyvernClient.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## 
## Workflows
<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">getFolders</a>({ ...params }) -> Skyvern.Folder[]</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Get all folders for the organization
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.getFolders({
    page: 1,
    page_size: 1,
    search: "search"
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**request:** `Skyvern.GetFoldersV1FoldersGetRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">createFolder</a>({ ...params }) -> Skyvern.Folder</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Create a new folder to organize workflows
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.createFolder({
    title: "title"
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**request:** `Skyvern.FolderCreate` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">getFolder</a>(folderId) -> Skyvern.Folder</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Get a specific folder by ID
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.getFolder("fld_123");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**folderId:** `string` — Folder ID
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">updateFolder</a>(folderId, { ...params }) -> Skyvern.Folder</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Update a folder's title or description
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.updateFolder("fld_123");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**folderId:** `string` — Folder ID
    
</dd>
</dl>

<dl>
<dd>

**request:** `Skyvern.FolderUpdate` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">deleteFolder</a>(folderId, { ...params }) -> Record<string, unknown></code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Delete a folder. Optionally delete all workflows in the folder.
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.deleteFolder("fld_123", {
    delete_workflows: true
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**folderId:** `string` — Folder ID
    
</dd>
</dl>

<dl>
<dd>

**request:** `Skyvern.DeleteFolderV1FoldersFolderIdDeleteRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.workflows.<a href="/src/api/resources/workflows/client/Client.ts">updateWorkflowFolder</a>(workflowPermanentId, { ...params }) -> Skyvern.Workflow</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Update a workflow's folder assignment for the latest version
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.workflows.updateWorkflowFolder("wpid_123");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**workflowPermanentId:** `string` — Workflow permanent ID
    
</dd>
</dl>

<dl>
<dd>

**request:** `Skyvern.UpdateWorkflowFolderRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Workflows.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## BrowserProfiles
<details><summary><code>client.browserProfiles.<a href="/src/api/resources/browserProfiles/client/Client.ts">listBrowserProfiles</a>({ ...params }) -> Skyvern.BrowserProfile[]</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Get all browser profiles for the organization
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.browserProfiles.listBrowserProfiles({
    include_deleted: true
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**request:** `Skyvern.ListBrowserProfilesV1BrowserProfilesGetRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `BrowserProfiles.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.browserProfiles.<a href="/src/api/resources/browserProfiles/client/Client.ts">createBrowserProfile</a>({ ...params }) -> Skyvern.BrowserProfile</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.browserProfiles.createBrowserProfile({
    name: "name"
});

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**request:** `Skyvern.CreateBrowserProfileRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `BrowserProfiles.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.browserProfiles.<a href="/src/api/resources/browserProfiles/client/Client.ts">getBrowserProfile</a>(profileId) -> Skyvern.BrowserProfile</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Get a specific browser profile by ID
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.browserProfiles.getBrowserProfile("bp_123456");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**profileId:** `string` — The ID of the browser profile. browser_profile_id starts with `bp_`
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `BrowserProfiles.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.browserProfiles.<a href="/src/api/resources/browserProfiles/client/Client.ts">deleteBrowserProfile</a>(profileId) -> void</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Delete a browser profile (soft delete)
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.browserProfiles.deleteBrowserProfile("bp_123456");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**profileId:** `string` — The ID of the browser profile to delete. browser_profile_id starts with `bp_`
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `BrowserProfiles.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## Scripts
<details><summary><code>client.scripts.<a href="/src/api/resources/scripts/client/Client.ts">runScript</a>(scriptId) -> unknown</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Run a script
</dd>
</dl>
</dd>
</dl>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.scripts.runScript("s_abc123");

```
</dd>
</dl>
</dd>
</dl>

#### ⚙️ Parameters

<dl>
<dd>

<dl>
<dd>

**scriptId:** `string` — The unique identifier of the script
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Scripts.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>
