#!/bin/bash

# Call function to send telemetry event
log_event() {
    if [ -n $1 ]; then
        python skyvern/analytics.py $1
    fi
}

# Function to check if a command exists
command_exists() {
    command -v "$1" &> /dev/null
}

# Ensure required commands are available
for cmd in poetry python3.11; do
    if ! command_exists "$cmd"; then
        echo "Error: $cmd is not installed." >&2
        exit 1
    fi
done

# Function to initialize .env file
initialize_env_file() {
    if [ -f ".env" ]; then
        echo ".env file already exists, skipping initialization."
        return
    fi

    echo "Initializing .env file..."
    cp .env.example .env

    # Ask for OpenAI API key
    read -p "Please enter your OpenAI API key for GPT4V (this will be stored only in your local .env file): " openai_api_key
    awk -v key="$openai_api_key" '{gsub(/OPENAI_API_KEYS=\["abc","def","ghi"\]/, "OPENAI_API_KEYS=[\"" key "\"]"); print}' .env > .env.tmp && mv .env.tmp .env


    # Ask for email or generate UUID
    read -p "Please enter your email for analytics (press enter to skip): " analytics_id
    if [ -z "$analytics_id" ]; then
        analytics_id=$(uuidgen)
    fi
    awk -v id="$analytics_id" '{gsub(/ANALYTICS_ID="anonymous"/, "ANALYTICS_ID=\"" id "\""); print}' .env > .env.tmp && mv .env.tmp .env

    echo ".env file has been initialized."
}

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

# Choose python version
choose_python_version_or_fail() {
  poetry env use python3.11 || { echo "Error: Python 3.11 is not installed."; exit 1; }
}


# Function to install dependencies
install_dependencies() {
    poetry install
}

activate_poetry_env() {
    source "$(poetry env info --path)/bin/activate"
}

install_dependencies_after_poetry_env() {
    echo "Installing playwright dependencies..."
    playwright install
}

# Function to setup PostgreSQL
setup_postgresql() {
    echo "Installing postgresql using brew"
    if ! command_exists psql; then
        echo "`postgresql` is not installed."
        if [[ "$OSTYPE" != "darwin"* ]]; then
            echo "Error: Please install postgresql and start the service manually and re-run the script." >&2
            exit 1
        fi
        if ! command_exists brew; then
            echo "Error: brew is not installed, please install homebrew and re-run the script or install postgresql manually." >&2
            exit 1
        fi
        brew install postgresql@14
    fi
    brew services start postgresql@14

    if psql skyvern -U skyvern -c '\q'; then
        echo "Connection successful. Database and user exist."
    else
        createuser skyvern
        createdb skyvern -O skyvern
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
    echo -e "[skyvern]\nconfigs = [\n    {\"env\" = \"local\", \"host\" = \"http://0.0.0.0:8000/api/v1\", \"orgs\" = [{name=\"Skyvern\", cred=\"$api_token\"}]}\n]" > .streamlit/secrets.toml
    echo ".streamlit/secrets.toml file updated with organization details."
}

# Main function
main() {
    initialize_env_file
    choose_python_version_or_fail
    deactivate_and_remove_poetry_env
    install_dependencies
    setup_postgresql
    activate_poetry_env
    install_dependencies_after_poetry_env
    run_alembic_upgrade
    create_organization
    log_event "skyvern-oss-setup-complete"
    echo "Setup completed successfully."
}

# Execute main function
main
