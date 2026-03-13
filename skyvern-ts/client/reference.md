# Reference
<details><summary><code>client.<a href="/src/Client.ts">updateWorkflowFolder</a>(workflowPermanentId, { ...params }) -> Skyvern.Workflow</code></summary>
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
await client.updateWorkflowFolder("wpid_123");

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

**requestOptions:** `SkyvernClient.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

## 
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
