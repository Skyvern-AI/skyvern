<!-- DOC: README update for startup path clarification -->

# Skyvern

... (existing content) ...

## Local Development: Native vs Docker Compose

Skyvern supports two mutually exclusive methods for local development:

### 1. Native Server (`skyvern run server`)
- Uses the Postgres container started by the CLI wizard.
- Recommended if you want to quickly try out Skyvern natively.
- To start:
  ```bash
  skyvern run server
  ```

### 2. Docker Compose (`docker compose up -d`)
- Uses Docker Compose to manage all services, including its own Postgres container.
- Recommended for production-like environments or if you want to run all services together.
- To start:
  ```bash
  docker compose up -d
  ```

### ⚠️ Important: Do Not Run Both Postgres Containers Simultaneously
Running both the CLI-managed and Docker Compose-managed Postgres containers at the same time can cause:
- Port conflicts (both use 5432)
- Database state mismatches
- Troubleshooting headaches

**If you switch from `skyvern run server` to `docker compose up -d`, stop and remove the CLI-created Postgres container:**
```bash
docker rm -f postgresql-container
```

**If you switch from Docker Compose to native, stop the Compose stack first:**
```bash
docker compose down
```

### Quick Reference Table
| Scenario           | Command(s)                       | Notes                                    |
|--------------------|----------------------------------|------------------------------------------|
| Native server      | `skyvern run server`             | Uses CLI wizard's Postgres               |
| Docker Compose     | `docker compose up -d`           | Uses Compose-managed Postgres            |
| Remove CLI Postgres| `docker rm -f postgresql-container` | Run if switching to Compose            |
| Stop Compose stack | `docker compose down`            | Run if switching to native               |

For more details, see [issue #2218](https://github.com/Skyvern-AI/skyvern/issues/2218).

... (rest of README unchanged) ...
