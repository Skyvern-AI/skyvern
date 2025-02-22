import shutil
import subprocess
import time
from typing import Optional

import typer

from skyvern.utils import migrate_db

app = typer.Typer()


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def run_command(command: str, check: bool = True) -> tuple[Optional[str], Optional[int]]:
    try:
        result = subprocess.run(command, shell=True, check=check, capture_output=True, text=True)
        return result.stdout.strip(), result.returncode
    except subprocess.CalledProcessError as e:
        return None, e.returncode


def is_postgres_running() -> bool:
    if command_exists("pg_isready"):
        result, _ = run_command("pg_isready")
        return result is not None and "accepting connections" in result
    return False


def database_exists(dbname: str, user: str) -> bool:
    check_db_command = f'psql {dbname} -U {user} -c "\\q"'
    output, _ = run_command(check_db_command, check=False)
    return output is not None


def create_database_and_user() -> None:
    print("Creating database user and database...")
    run_command("createuser skyvern")
    run_command("createdb skyvern -O skyvern")
    print("Database and user created successfully.")


def is_docker_running() -> bool:
    if not command_exists("docker"):
        return False
    _, code = run_command("docker info", check=False)
    return code == 0


def is_postgres_running_in_docker() -> bool:
    _, code = run_command("docker ps | grep -q postgresql-container", check=False)
    return code == 0


def is_postgres_container_exists() -> bool:
    _, code = run_command("docker ps -a | grep -q postgresql-container", check=False)
    return code == 0


def setup_postgresql() -> None:
    print("Setting up PostgreSQL...")

    if command_exists("psql") and is_postgres_running():
        print("PostgreSQL is already running locally.")
        if database_exists("skyvern", "skyvern"):
            print("Database and user exist.")
        else:
            create_database_and_user()
        return

    if not is_docker_running():
        print("Docker is not running or not installed. Please install or start Docker and try again.")
        exit(1)

    if is_postgres_running_in_docker():
        print("PostgreSQL is already running in a Docker container.")
    else:
        print("Attempting to install PostgreSQL via Docker...")
        if not is_postgres_container_exists():
            run_command(
                "docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14"
            )
        else:
            run_command("docker start postgresql-container")
        print("PostgreSQL has been installed and started using Docker.")

        print("Waiting for PostgreSQL to start...")
        time.sleep(20)

    _, code = run_command('docker exec postgresql-container psql -U postgres -c "\\du" | grep -q skyvern', check=False)
    if code == 0:
        print("Database user exists.")
    else:
        print("Creating database user...")
        run_command("docker exec postgresql-container createuser -U postgres skyvern")

    _, code = run_command(
        "docker exec postgresql-container psql -U postgres -lqt | cut -d \\| -f 1 | grep -qw skyvern", check=False
    )
    if code == 0:
        print("Database exists.")
    else:
        print("Creating database...")
        run_command("docker exec postgresql-container createdb -U postgres skyvern -O skyvern")
        print("Database and user created successfully.")


@app.command(name="init")
def init(
    openai_api_key: str = typer.Option(..., help="The OpenAI API key"),
    log_level: str = typer.Option("CRITICAL", help="The log level"),
) -> None:
    setup_postgresql()
    # Generate .env file
    with open(".env", "w") as env_file:
        env_file.write("ENABLE_OPENAI=true\n")
        env_file.write(f"OPENAI_API_KEY={openai_api_key}\n")
        env_file.write(f"LOG_LEVEL={log_level}\n")
        env_file.write("ARTIFACT_STORAGE_PATH=./artifacts\n")
    print(".env file created with the parameters provided.")


@app.command(name="migrate")
def migrate() -> None:
    migrate_db()
