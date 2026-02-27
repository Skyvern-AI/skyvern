import asyncio
import subprocess
import sys

# Fix pentru Windows + psycopg
asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Rulează Skyvern ca modul CLI
subprocess.run([sys.executable, "-m", "skyvern"])
