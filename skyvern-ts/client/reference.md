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
