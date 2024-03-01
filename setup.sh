#!/bin/bash

# Function to check if a command exists
command_exists() {
    command -v "$1" &> /dev/null
}

# Ensure required commands are available
for cmd in poetry pre-commit brew python; do
    if ! command_exists "$cmd"; then
        echo "Error: $cmd is not installed." >&2
        exit 1
    fi
done

# Function to remove Poetry environment
remove_poetry_env() {
    local env_path
    env_path=$(poetry env info --path)
    if [ -d "$env_path" ]; then
        rm -rf "$env_path"
        echo "Removed the poetry environment at $env_path."
    else
        echo "No poetry environment found."
    fi
}

# Function to install dependencies
install_dependencies() {
    poetry install
    pre-commit install
}

activate_poetry_env() {
    source "$(poetry env info --path)/bin/activate"
}

# Function to setup PostgreSQL
setup_postgresql() {
    echo "Installing postgresql using brew"
    brew install postgresql@14
    brew services start postgresql@14

    if psql skyvern-open-source -U skyvern-open-source -c '\q'; then
        echo "Connection successful. Database and user exist."
    else
        createuser skyvern-open-source
        createdb skyvern-open-source -O skyvern-open-source
        echo "Database and user created successfully."
    fi
}

# Function to run Alembic upgrade
run_alembic_upgrade() {
    echo "Running Alembic upgrade..."
    alembic upgrade head
}

# Function to create organization and API token
create_organization() {
    echo "Creating organization and API token..."
    local org_output api_token
    org_output=$(python scripts/create_organization.py Skyvern-Open-Source)
    api_token=$(echo "$org_output" | awk '/token=/{gsub(/.*token='\''|'\''.*/, ""); print}')

    # Ensure .streamlit directory exists
    mkdir -p .streamlit

    # Check if secrets.toml exists and back it up
    if [ -f ".streamlit/secrets.toml" ]; then
        mv .streamlit/secrets.toml .streamlit/secrets.backup.toml
        echo "Existing secrets.toml file backed up as secrets.backup.toml"
    fi

    # Update the secrets-open-source.toml file
    echo -e "[skyvern]\nconfigs = [\n    {\"env\" = \"local\", \"host\" = \"http://0.0.0.0:8000/api/v1\", \"orgs\" = [{name=\"Skyvern-Open-Source\", cred=\"$api_token\"}]}\n]" > .streamlit/secrets.toml
    echo ".streamlit/secrets.toml file updated with organization details."
}

# Main function
main() {
    remove_poetry_env
    install_dependencies
    setup_postgresql
    activate_poetry_env
    run_alembic_upgrade
    create_organization
    echo "Setup completed successfully."
}

# Execute main function
main