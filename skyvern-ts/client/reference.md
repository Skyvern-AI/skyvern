# Reference
<details><summary><code>client.<a href="/src/Client.ts">changeTierApiV1BillingChangeTierPost</a>({ ...params }) -> Skyvern.ChangeTierResponse</code></summary>
<dl>
<dd>

#### 📝 Description

<dl>
<dd>

<dl>
<dd>

Redirect to Stripe Portal for tier changes.
Portal handles proration based on configured settings:
- Upgrades: Immediate proration charge
- Downgrades: Apply at end of billing period
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
await client.changeTierApiV1BillingChangeTierPost({
    tier: "free"
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

**request:** `Skyvern.ChangeTierRequest` 
    
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

## Agent
<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">listOrganizationSchedules</a>({ ...params }) -> Skyvern.OrganizationScheduleListResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.listOrganizationSchedules({
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

**request:** `Skyvern.ListOrganizationSchedulesApiV1SchedulesGetRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">listWorkflowSchedules</a>(workflowPermanentId) -> Skyvern.WorkflowScheduleListResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.listWorkflowSchedules("workflow_permanent_id");

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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">createWorkflowSchedule</a>(workflowPermanentId, { ...params }) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.createWorkflowSchedule("workflow_permanent_id", {
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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">getWorkflowSchedule</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.getWorkflowSchedule("workflow_permanent_id", "workflow_schedule_id");

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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">updateWorkflowSchedule</a>(workflowPermanentId, workflowScheduleId, { ...params }) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.updateWorkflowSchedule("workflow_permanent_id", "workflow_schedule_id", {
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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">deleteWorkflowScheduleRoute</a>(workflowPermanentId, workflowScheduleId) -> Record&lt;string, boolean&gt;</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.deleteWorkflowScheduleRoute("workflow_permanent_id", "workflow_schedule_id");

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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">enableWorkflowSchedule</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.enableWorkflowSchedule("workflow_permanent_id", "workflow_schedule_id");

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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>

<details><summary><code>client.agent.<a href="/src/api/resources/agent/client/Client.ts">disableWorkflowSchedule</a>(workflowPermanentId, workflowScheduleId) -> Skyvern.WorkflowScheduleResponse</code></summary>
<dl>
<dd>

#### 🔌 Usage

<dl>
<dd>

<dl>
<dd>

```typescript
await client.agent.disableWorkflowSchedule("workflow_permanent_id", "workflow_schedule_id");

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

**requestOptions:** `Agent.RequestOptions` 
    
</dd>
</dl>
</dd>
</dl>


</dd>
</dl>
</details>
