# Reference
<details><summary><code>client.<a href="/src/Client.ts">changeTierApiV1BillingChangeTierPost</a>({ ...params }) -> Testcharmvision.ChangeTierResponse</code></summary>
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

**request:** `Testcharmvision.ChangeTierRequest` 
    
</dd>
</dl>

<dl>
<dd>

**requestOptions:** `TestcharmvisionClient.RequestOptions` 
    
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
