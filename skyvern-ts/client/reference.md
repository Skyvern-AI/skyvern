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
## Artifacts
<details><summary><code>client.artifacts.<a href="/src/api/resources/artifacts/client/Client.ts">getArtifactContent</a>(artifactId) -> unknown</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Download the raw content of an artifact (supports bundled artifacts).
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
await client.artifacts.getArtifactContent("artifact_id");

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

**artifactId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Artifacts.RequestOptions` 
    
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

## Schedules
<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">listAll</a>({ ...params }) -> Skyvern.OrganizationScheduleListResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.listAll({
    page: 1,
    page_size: 1,
    status: "active",
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

**request:** `Skyvern.SchedulesListAllRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">list</a>(workflowPermanentId) -> Skyvern.WorkflowScheduleListResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.list("workflow_permanent_id");

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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">create</a>(workflowPermanentId, { ...params }) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.create("workflow_permanent_id", {
    cron_expression: "cron_expression",
    timezone: "timezone"
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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**request:** `Skyvern.WorkflowScheduleUpsertRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">get</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.get("workflow_permanent_id", "workflow_schedule_id");

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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**workflowScheduleId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">update</a>(workflowPermanentId, workflowScheduleId, { ...params }) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.update("workflow_permanent_id", "workflow_schedule_id", {
    cron_expression: "cron_expression",
    timezone: "timezone"
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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**workflowScheduleId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**request:** `Skyvern.WorkflowScheduleUpsertRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">delete</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.DeleteScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.delete("workflow_permanent_id", "workflow_schedule_id");

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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**workflowScheduleId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">enable</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.enable("workflow_permanent_id", "workflow_schedule_id");

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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**workflowScheduleId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.schedules.<a href="/src/api/resources/schedules/client/Client.ts">disable</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.schedules.disable("workflow_permanent_id", "workflow_schedule_id");

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

**workflowPermanentId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**workflowScheduleId:** `string` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Schedules.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>
