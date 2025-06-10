

# ─── Configuration ─────────────────────────────────────────────────────────────
SERVICE_ACCOUNT_TOKEN = "ops_eyJzaWduSW5BZGRyZXNzIjoibXkuMXBhc3N3b3JkLmNvbSIsInVzZXJBdXRoIjp7Im1ldGhvZCI6IlNSUGctNDA5NiIsImFsZyI6IlBCRVMyZy1IUzI1NiIsIml0ZXJhdGlvbnMiOjY1MDAwMCwic2FsdCI6IlFHTExzR0RJT3NIX3BDZXYxTEMtbUEifSwiZW1haWwiOiIzYm5mcGdheXJ4YmFlQDFwYXNzd29yZHNlcnZpY2VhY2NvdW50cy5jb20iLCJzcnBYIjoiMDkwNzZmMDEwZWVlY2QzYzRlMjViZTc5ZmViNGYyNTM5OTc1YTI1NTg1ZTNkOGJkZGE4MThmNjkyNDE4ZWNjMyIsIm11ayI6eyJhbGciOiJBMjU2R0NNIiwiZXh0Ijp0cnVlLCJrIjoiaFJ3OHJtRmVwQ0RMMnU5V0FLY1ptZXJrREljcmwtd19ld3k1cDcyeTc2cyIsImtleV9vcHMiOlsiZW5jcnlwdCIsImRlY3J5cHQiXSwia3R5Ijoib2N0Iiwia2lkIjoibXAifSwic2VjcmV0S2V5IjoiQTMtR0tZTDIyLVhaQUFMRi1ZWkRQSy1MWFpZUi1aWVM1Mi1CM1hGQyIsInRocm90dGxlU2VjcmV0Ijp7InNlZWQiOiJjYzBhNTVlZDg1MTQ1OGU4ZjYyNTU0M2VhYWEwOTJhZWVhNmNmMTU0NWU1N2FlZDBlM2Y4YWZjZTA4Y2Q0ZTEzIiwidXVpZCI6IkQ0NVNWWUpLSzVENlRMSTJCMjRUWjNVVlZRIn0sImRldmljZVV1aWQiOiJxNTY1cWltMmNtY3hlcmtjYjNvczIyd2o2ZSJ9"
VAULT_ID = "hdqnc4iwajd63vc6iuvy24zzqa"
ITEM_ID = "sdg6whox7qea3kplbikezmxhlu"
import asyncio
import os, json
from onepassword.client import Client

# ─── Main logic ────────────────────────────────────────────────────────────────
async def main():
    if not SERVICE_ACCOUNT_TOKEN:
        raise RuntimeError("Please set the OP_SERVICE_ACCOUNT_TOKEN environment variable.")

    # 1) Authenticate
    client = await Client.authenticate(
        auth=SERVICE_ACCOUNT_TOKEN,
        integration_name="My 1Password Integration",
        integration_version="v1.0.0",
    )

    # 2) Fetch the full item record (flat model in v0.3.0+)
    item = await client.items.get(VAULT_ID, ITEM_ID)

    # 3) Print the entire JSON payload
    # Option A: as a Python dict
    print(json.dumps(item.dict(), indent=2))

    # Option B: as the model’s built-in JSON
    # print(item.json(indent=2))


if __name__ == "__main__":
    asyncio.run(main())