from skyvern import Skyvern
from functools import lru_cache


async def get_api_key() -> None:

    from skyvern.config import settings
    from skyvern.forge import app
    from skyvern.forge.sdk.db.enums import OrganizationAuthTokenType
    from skyvern.library import Skyvern


    skyvern = Skyvern(base_url=settings.SKYVERN_BASE_URL, api_key=settings.SKYVERN_API_KEY)
    organization = await skyvern.get_organization()
    org_auth_token = await app.DATABASE.get_valid_org_auth_token(
        organization_id=organization.organization_id,
        token_type=OrganizationAuthTokenType.api,
    )
    
    token = org_auth_token.token if org_auth_token else ""

    print(token)


async def ping(skyvern: Skyvern) -> None:

    org = await skyvern.get_organization()

    if not org:
        raise Exception("Failed to get organization")

    print(org.organization_name)


@lru_cache(maxsize=1)
def get_model_names() -> list[str]:
    from litellm import model_cost 
    return list(sorted(model_cost.keys()))


def check_settings() -> None:
    from skyvern.config import settings
    print(settings.SKYVERN_LLM_NAME)


async def run_task(skyvern: Skyvern) -> None:
    from skyvern import LLM 

    run = await skyvern.run_task(
        prompt="Find the top post on hackernews today",
        llm=LLM(model_name="together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo"),
    )

    status = run.status

    print("Model:")
    print(run.model_config)
    print(run.run_request.model)
    print("\n")

    while status in ("pending", "queued", "running"):
        print(status)
        await asyncio.sleep(5)
        run = await skyvern.get_run(run_id=run.run_id)
        status = run.status

    print("\nFinal status:")    
    print(status)
    print("\nFailure reason:")
    print(run.failure_reason)
    print("\nOutput:")
    print(run.output)

if __name__ == "__main__":
    import asyncio 
    
    from skyvern.config import settings

    browser_path = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    
    skyvern = Skyvern(
        api_key=settings.SKYVERN_API_KEY, 
        base_url="http://localhost:8000",
        browser_path=browser_path,
    )

    # asyncio.run(ping(skyvern))
    # print(get_model_names())
    # check_settings()
    asyncio.run(run_task(skyvern))
    # asyncio.run(get_api_key())
    
    
    print("Done.")
