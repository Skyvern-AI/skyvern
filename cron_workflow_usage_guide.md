# Using Cron Jobs to Automate Workflows in Skyvern

Automated scheduling lets you run any Skyvern workflow at fixed times without writing extra code.  
This guide shows how to enable, configure, and manage cron schedules **via the UI _and_ the API**.

---

## 1  Prerequisites

* You have a published workflow in your organization
* Your account/role permits editing that workflow
* Skyvern backend v ≥ 2025-05-29 (cron support) and the UI ≥ v0.7.0

---

## 2  Configuring a Schedule in the UI

1. Open **Workflows → _Your workflow_**
2. Click the **Schedule Workflow** card (clock icon)

| Setting | Description |
|---------|-------------|
| **Enable Scheduling** | Master switch (cron_enabled) |
| **Cron Expression**  | 5-field cron string (* * * * *) |
| **Timezone** | IANA zone used to evaluate the expression |
| **Quick Examples** | One-click presets (hourly, daily, etc.) |

3. Pick or type a cron expression (see Section 5)
4. Select the correct timezone
5. Press **Save Schedule**

> Tip: Disable the toggle to pause the schedule without losing your cron string.

---

## 3  Configuring via API

Base path: `https://api.skyvern.ai/v1`

### 3.1  Get current schedule

```bash
curl -H "Authorization: Bearer $TOKEN" \
     https://api.skyvern.ai/v1/workflows/scheduler/<WORKFLOW_ID>
```

### 3.2  Set / update schedule

```bash
curl -X PUT -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
     -d '{
           "cron_expression": "0 3 1 * *",
           "timezone": "UTC",
           "cron_enabled": true
         }' \
     https://api.skyvern.ai/v1/workflows/scheduler/<WORKFLOW_ID>
```

### 3.3  Trigger immediately (ignore schedule)

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" \
     https://api.skyvern.ai/v1/workflows/scheduler/<WORKFLOW_ID>/trigger
```

---

## 4  Cron Expression Cheat-Sheet

Expression | Meaning
-----------|---------
`*/15 * * * *` | Every 15 minutes
`0 * * * *` | Hourly on the hour
`0 9 * * 1-5` | Weekdays at 09:00
`0 0 * * *` | Every midnight
`0 3 1 * *` | 1st of month at 03:00
`0 8 1 1 *` | Every 1 Jan at 08:00

Validate expressions at https://crontab.guru

---

## 5  What Happens Under the Hood

1. **WorkflowScheduler** (APScheduler) loads the new cron trigger
2. `next_run_time` column is updated—visible in UI/API
3. At fire time, a **workflow run** is created with `triggered_by_cron = true`
4. Normal execution & webhooks follow

---

## 6  Troubleshooting

| Symptom | Possible Cause | Fix |
|---------|----------------|-----|
| **Job never runs** | `cron_enabled` still `false` | Toggle on & save |
| **Next run time missing** | Invalid cron string | Re-enter a valid expression |
| **Wrong local time** | Mis-matched timezone | Choose correct `timezone` |

Happy scheduling!

