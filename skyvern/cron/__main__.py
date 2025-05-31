import asyncio

from dotenv import load_dotenv

from skyvern.services.cron_scheduler import start_scheduler

if __name__ == "__main__":
    load_dotenv()
    asyncio.run(start_scheduler())
