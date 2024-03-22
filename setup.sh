#!/bin/bash

# Call function to send telemetry event
log_event() {
    if [ -n $1 ]; then
        poetry run python skyvern/analytics.py $1
    fi
}

# Function to check if a command exists
command_exists() {
    command -v "$1" &> /dev/null
}

# Ensure required commands are available
check_if_required_commands_exist() {
    local missing_commands=()
    for cmd in poetry python3.11; do
        if ! command_exists "$cmd"; then
            missing_commands+=("$cmd")
        fi
    done

    if [ ${#missing_commands[@]} -gt 0 ]; then
        echo "Error: The following commands are required but not found: ${missing_commands[*]}"
        exit 1
    fi
}

# Function to update or add environment variable in .env file
update_or_add_env_var() {
    local key=$1
    local value=$2
    if grep -q "^$key=" .env; then
        # Update existing variable
        if ! sed -i.bak "s/^$key=.*/$key=$value/" .env && rm -f .env.bak; then
            echo "Error: Failed to update $key in .env."
            return 1
        fi
    else
        # Add new variable
        if ! echo "$key=$value" >> .env; then
            echo "Error: Failed to add $key to .env."
            return 1
        fi
    fi
}

# Function to set up LLM provider environment variables
setup_llm_providers() {
    echo "Configuring Large Language Model (LLM) Providers..."
    echo "Note: All information provided here will be stored only on your local machine."
    local model_options=()
    local failed=false

    # OpenAI Configuration
    echo "To enable OpenAI, you must have an OpenAI API key."
    read -p "Do you want to enable OpenAI (y/n)? " enable_openai
    if [[ "$enable_openai" == "y" ]]; then
        read -p "Enter your OpenAI API key: " openai_api_key
        if [ -z "$openai_api_key" ]; then
            echo "Error: OpenAI API key is required."
            echo "OpenAI will not be enabled."
        else
            if update_or_add_env_var "OPENAI_API_KEY" "$openai_api_key" && \
               update_or_add_env_var "ENABLE_OPENAI" "true"; then
                model_options+=("OPENAI_GPT4_TURBO" "OPENAI_GPT4V")
            else
                failed=true
            fi
        fi
    else
        if ! update_or_add_env_var "ENABLE_OPENAI" "false"; then
            failed=true
        fi
    fi

    # Anthropic Configuration
    echo "To enable Anthropic, you must have an Anthropic API key."
    read -p "Do you want to enable Anthropic (y/n)? " enable_anthropic
    if [[ "$enable_anthropic" == "y" ]]; then
        read -p "Enter your Anthropic API key: " anthropic_api_key
        if [ -z "$anthropic_api_key" ]; then
            echo "Error: Anthropic API key is required."
            echo "Anthropic will not be enabled."
        else
            if update_or_add_env_var "ANTHROPIC_API_KEY" "$anthropic_api_key" && \
               update_or_add_env_var "ENABLE_ANTHROPIC" "true"; then
                model_options+=("ANTHROPIC_CLAUDE3")
            else
                failed=true
            fi
        fi
    else
        if ! update_or_add_env_var "ENABLE_ANTHROPIC" "false"; then
            failed=true
        fi
    fi

    # Azure Configuration
    echo "To enable Azure, you must have an Azure deployment name, API key, base URL, and API version."
    read -p "Do you want to enable Azure (y/n)? " enable_azure
    if [[ "$enable_azure" == "y" ]]; then
        read -p "Enter your Azure deployment name: " azure_deployment
        read -p "Enter your Azure API key: " azure_api_key
        read -p "Enter your Azure API base URL: " azure_api_base
        read -p "Enter your Azure API version: " azure_api_version
        if [ -z "$azure_deployment" ] || [ -z "$azure_api_key" ] || [ -z "$azure_api_base" ] || [ -z "$azure_api_version" ]; then
            echo "Error: All Azure fields must be populated."
            echo "Azure will not be enabled."
        else
            if update_or_add_env_var "AZURE_DEPLOYMENT" "$azure_deployment" && \
               update_or_add_env_var "AZURE_API_KEY" "$azure_api_key" && \
               update_or_add_env_var "AZURE_API_BASE" "$azure_api_base" && \
               update_or_add_env_var "AZURE_API_VERSION" "$azure_api_version" && \
               update_or_add_env_var "ENABLE_AZURE" "true"; then
                model_options+=("AZURE_OPENAI_GPT4V")
            else
                failed=true
            fi
        fi
    else
        if ! update_or_add_env_var "ENABLE_AZURE" "false"; then
            failed=true
        fi
    fi

    # Model Selection
    if [ ${#model_options[@]} -eq 0 ]; then
        echo "No LLM providers enabled. You won't be able to run Skyvern unless you enable at least one provider. You can re-run this script to enable providers or manually update the .env file."
    else
        echo "Available LLM models based on your selections:"
        for i in "${!model_options[@]}"; do
            echo "$((i+1)). ${model_options[$i]}"
        done
        read -p "Choose a model by number (e.g., 1 for ${model_options[0]}): " model_choice
        chosen_model=${model_options[$((model_choice-1))]}
        echo "Chosen LLM Model: $chosen_model"
        if ! update_or_add_env_var "LLM_KEY" "$chosen_model"; then
            failed=true
        fi
    fi

    if [ "$failed" = true ]; then
        echo "Error: Failed to update .env file with LLM provider configurations."
        return 1
    fi
    echo "LLM provider configurations updated in .env."
}


# Function to initialize .env file
initialize_env_file() {
    if [ -f ".env" ]; then
        echo ".env file already exists, skipping initialization."
        read -p "Do you want to go through LLM provider setup again (y/n)? " redo_llm_setup
        if [[ "$redo_llm_setup" == "y" ]]; then
            return setup_llm_providers
        else
            return 0
        fi
    fi

    echo "Initializing .env file..."
    # check if cp fails
    if ! cp .env.example .env; then
        echo "Error: Failed to copy .env.example to .env."
        return 1
    fi

    if ! setup_llm_providers; then
        echo "Error: Failed to setup LLM providers."
        return 1
    fi

    # Ask for email or generate UUID
    read -p "Please enter your email for analytics (press enter to skip): " analytics_id
    if [ -z "$analytics_id" ]; then
        analytics_id=$(uuidgen)
    fi
    update_or_add_env_var "ANALYTICS_ID" "$analytics_id"
    echo ".env file has been initialized."
}

# Function to remove Poetry environment
remove_poetry_env() {
    local env_path
    env_path=$(poetry env info --path)
    if [ -d "$env_path" ]; then
        if ! rm -rf "$env_path"; then
            echo "Error: Failed to remove the poetry environment at $env_path."
            return 1
        else
            echo "Removed the poetry environment at $env_path."
        fi
    else
        echo "No poetry environment found."
    fi
}

# Choose python version
choose_python_version_or_fail() {
  if ! poetry env use python3.11; then
    echo "Error: Python 3.11 is required. Please install it and try again."
    return 1
  fi
}


# Function to install dependencies
install_dependencies() {
    if ! poetry install; then
        echo "Error: Failed to install dependencies."
        return 1
    fi
}

activate_poetry_env() {
    if ! source "$(poetry env info --path)/bin/activate"; then
        echo "Error: Failed to activate poetry environment."
        return 1
    fi
}

install_dependencies_after_poetry_env() {
    if ! playwright install; then
        echo "Error: Failed to install Playwright dependencies."
        return 1
    fi
}

# Function to setup PostgreSQL
setup_postgresql() {
    echo "Installing postgresql using brew"

    # Attempt to connect to the default PostgreSQL service if it's already running via psql
    if command_exists psql; then
        if pg_isready; then
            echo "PostgreSQL is already running locally."
            # Assuming the local PostgreSQL setup is ready for use
            if psql skyvern -U skyvern -c '\q'; then
                echo "Connection successful. Database and user exist."
            else
                if createuser skyvern && createdb skyvern -O skyvern; then
                    echo "Database and user created successfully."
                else
                    echo "Error: Failed to create database and user."
                    return 1
                fi
            fi
            return 0
        fi
    fi
    
    # Check if Docker is installed and running
    if ! command_exists docker || ! docker info > /dev/null 2>&1; then
        echo "Docker is not running or not installed. Please install or start Docker and try again."
        return 1
    fi

    # Check if PostgreSQL is already running in a Docker container
    if docker ps | grep -q postgresql-container; then
        echo "PostgreSQL is already running in a Docker container."
    else 
        # Attempt to install and start PostgreSQL using Docker
        echo "Attempting to install PostgreSQL via Docker..."
        if ! docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14; then
            echo "Error: Failed to install and start PostgreSQL using Docker."
            return 1
        fi
        echo "PostgreSQL has been installed and started using Docker."

        # Wait for PostgreSQL to start
        echo "Waiting for PostgreSQL to start..."
        sleep 20  # Adjust sleep time as necessary
    fi

    # Assuming docker exec works directly since we've checked Docker's status before
    if docker exec postgresql-container psql -U postgres -c "\du" | grep -q skyvern; then
        echo "Database user exists."
    else
        echo "Creating database user..."
        docker exec postgresql-container createuser -U postgres skyvern
    fi

    if docker exec postgresql-container psql -U postgres -lqt | cut -d \| -f 1 | grep -qw skyvern; then
        echo "Database exists."
    else
        echo "Creating database..."
        docker exec postgresql-container createdb -U postgres skyvern -O skyvern
        echo "Database and user created successfully."
    fi
}

# Function to run Alembic upgrade
run_alembic_upgrade() {
    if ! alembic upgrade head; then
        return 1
    fi
}

# Function to create organization and API token
create_organization() {
    echo "Creating organization and API token..."
    local org_output api_token
    org_output=$(poetry run python scripts/create_organization.py Skyvern-Open-Source)
    is_org_created=$?
    if [ is_org_created -ne 0 ]; then
        echo "Error: Failed to create organization and API token."
        return 1
    fi
    api_token=$(echo "$org_output" | awk '/token=/{gsub(/.*token='\''|'\''.*/, ""); print}')

    # Ensure .streamlit directory exists
    if ! mkdir -p .streamlit; then
        echo "Error: Failed to create .streamlit directory."
        return 1
    fi

    # Check if secrets.toml exists and back it up
    if [ -f ".streamlit/secrets.toml" ]; then
        if mv .streamlit/secrets.toml .streamlit/secrets.backup.toml; then
            echo "Existing .streamlit/secrets.toml file backed up as .streamlit/secrets.backup.toml"
        else
            echo "Error: Failed to back up existing .streamlit/secrets.toml file."
            return 1
        fi
    fi

    # Update the secrets-open-source.toml file
    if ! echo -e "[skyvern]\nconfigs = [\n    {\"env\" = \"local\", \"host\" = \"http://0.0.0.0:8000/api/v1\", \"orgs\" = [{name=\"Skyvern\", cred=\"$api_token\"}]}\n]" > .streamlit/secrets.toml; then
        echo "Error: Failed to update .streamlit/secrets.toml file."
        return 1
    else
        echo ".streamlit/secrets.toml file updated with organization details."
    fi
}

# Main function
main() {
    echo "Setting up Skyvern..."
    echo "Checking for required commands exist..."
    check_if_required_commands_exist || { echo "Error: Required commands are missing. Please install them and try again."; return 1; }
    echo "Initializing environment variables..."
    initialize_env_file || { echo "Error: Failed to initialize .env file."; return 1; }
    echo "Choosing Python version..."
    choose_python_version_or_fail || { echo "Error: Failed to choose Python version."; return 1; }
    echo "Removing poetry environment..."
    remove_poetry_env || { echo "Error: Failed to remove poetry environment."; return 1; }
    echo "Installing dependencies..."
    install_dependencies || { echo "Error: Failed to install dependencies."; return 1; }
    echo "Setting up PostgreSQL..."
    setup_postgresql || { echo "Error: Failed to setup PostgreSQL."; return 1; }
    echo "Activating poetry environment..."
    activate_poetry_env || { echo "Error: Failed to activate poetry environment."; return 1; }
    echo "Installing dependencies after activating poetry environment..."
    install_dependencies_after_poetry_env || { echo "Error: Failed to install dependencies after activating poetry environment."; return 1; }
    echo "Running Alembic upgrade..."
    run_alembic_upgrade || { echo "Error: Failed to run Alembic upgrade."; return 1; }
    echo "Creating organization and API token..."
    create_organization || { echo "Error: Failed to create organization and API token."; return 1; }
    log_event "skyvern-oss-setup-complete"
    echo "Setup completed successfully."
}

# Execute main function
main
