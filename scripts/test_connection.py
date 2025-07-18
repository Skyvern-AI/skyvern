from dotenv import load_dotenv
import os
from sqlalchemy import create_engine

load_dotenv()
db_url = os.getenv("DATABASE_URL")
print("DATABASE_URL:", db_url)
engine = create_engine(db_url)
with engine.connect() as conn:
    print("Connection successful!")
