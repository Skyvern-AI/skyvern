#!/bin/bash

# Function to check if a command exists
command_exists() {
    command -v "$1" &> /dev/null
}

ensure_required_commands() {
    # Ensure required commands are available
    for cmd in poetry npm; do
        if ! command_exists "$cmd"; then
            echo "Error: $cmd is not installed." >&2
            exit 1
        fi
    done
}

# Function to update or add environment variable in .env file
update_or_add_env_var() {
    local key=$1
    local value=$2
    local temp_file=$(mktemp)
    local found=0
    
    if [ ! -f ".env" ]; then
        echo "$key=$value" > .env
        return
    fi
    
    while IFS= read -r line || [ -n "$line" ]; do
        if [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]]; then
            echo "$line" >> "$temp_file"
            continue
        fi
        
        if [[ "$line" =~ ^[[:space:]]*"$key"= ]]; then
            echo "$key=$value" >> "$temp_file"
            found=1
        else
            echo "$line" >> "$temp_file"
        fi
    done < .env
    
    if [ $found -eq 0 ]; then
        echo "$key=$value" >> "$temp_file"
    fi
    
    mv "$temp_file" .env
}


# Function to check and fix database string to use 'postgres' instead of 'localhost'
fix_database_string() {
    echo "Checking and fixing DATABASE_STRING..."
    if grep -q "DATABASE_STRING=" .env; then
        # Replace 'localhost' with 'postgres' in the DATABASE_STRING
        sed -i.bak "s/postgresql+psycopg:\/\/skyvern:skyvern@localhost\/skyvern/postgresql+psycopg:\/\/skyvern:skyvern@postgres\/skyvern/" .env && rm -f .env.bak
        echo "DATABASE_STRING updated to use 'postgres' instead of 'localhost'."
    else
        # Add DATABASE_STRING if it doesn't exist
        echo "DATABASE_STRING=\"postgresql+psycopg://skyvern:skyvern@postgres/skyvern\"" >> .env
        echo "DATABASE_STRING added with postgres host."
    fi
}

# Function to set up LLM provider environment variables
setup_llm_providers() {
    echo "Configuring Large Language Model (LLM) Providers..."
    echo "Note: All information provided here will be stored only on your local machine."
    local model_options=()

    # OpenAI Configuration
    echo "To enable OpenAI, you must have an OpenAI API key."
    read -p "Do you want to enable OpenAI (y/n)? " enable_openai
    if [[ "$enable_openai" == "y" ]]; then
        read -p "Enter your OpenAI API key: " openai_api_key
        if [ -z "$openai_api_key" ]; then
            echo "Error: OpenAI API key is required."
            echo "OpenAI will not be enabled."
        else
            update_or_add_env_var "OPENAI_API_KEY" "$openai_api_key"
            update_or_add_env_var "ENABLE_OPENAI" "true"
            model_options+=(
                "OPENAI_GPT4_1"
                "OPENAI_GPT4_1_MINI"
                "OPENAI_GPT4_1_NANO"
                "OPENAI_GPT4O"
                "OPENAI_O4_MINI"
                "OPENAI_O3"
            )
        fi
    else
        update_or_add_env_var "ENABLE_OPENAI" "false"
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
            update_or_add_env_var "ANTHROPIC_API_KEY" "$anthropic_api_key"
            update_or_add_env_var "ENABLE_ANTHROPIC" "true"
            model_options+=(
                "ANTHROPIC_CLAUDE3.5_SONNET"
                "ANTHROPIC_CLAUDE3.7_SONNET"
            )
        fi
    else
        update_or_add_env_var "ENABLE_ANTHROPIC" "false"
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
            update_or_add_env_var "AZURE_DEPLOYMENT" "$azure_deployment"
            update_or_add_env_var "AZURE_API_KEY" "$azure_api_key"
            update_or_add_env_var "AZURE_API_BASE" "$azure_api_base"
            update_or_add_env_var "AZURE_API_VERSION" "$azure_api_version"
            update_or_add_env_var "ENABLE_AZURE" "true"
            model_options+=(
                "AZURE_OPENAI_GPT4O"
            )
        fi
    else
        update_or_add_env_var "ENABLE_AZURE" "false"
    fi

    #Gemini Configuartion
    echo "To enable Gemini, you must have an Gemini API key."
    read -p "Do you want to enable Gemini (y/n)? " enable_gemini
    if [[ "$enable_gemini" == "y" ]]; then
        read -p "Enter your Gemini API key: " gemini_api_key
        if [ -z "$gemini_api_key" ]; then
            echo "Error: Gemini API key is required."
            echo "Gemini will not be enabled."
        else
            update_or_add_env_var "GEMINI_API_KEY" "$gemini_api_key"
            update_or_add_env_var "ENABLE_GEMINI" "true"
            model_options+=(
                "GEMINI_FLASH_2_0"
                "GEMINI_FLASH_2_0_LITE"
                "GEMINI_2.5_PRO_PREVIEW_03_25"
                "GEMINI_2.5_PRO_EXP_03_25"
            )
        fi
    else
        update_or_add_env_var "ENABLE_GEMINI" "false"
    fi

    # Novita AI Configuration
    echo "To enable Novita AI, you must have an Novita AI API key."
    read -p "Do you want to enable Novita AI (y/n)? " enable_novita
    if [[ "$enable_novita" == "y" ]]; then
        read -p "Enter your Novita AI API key: " novita_api_key
        if [ -z "$novita_api_key" ]; then
            echo "Error: Novita AI API key is required."
            echo "Novita AI will not be enabled."
        else
            update_or_add_env_var "NOVITA_API_KEY" "$novita_api_key"
            update_or_add_env_var "ENABLE_NOVITA" "true"
            model_options+=(
                "NOVITA_DEEPSEEK_R1"
                "NOVITA_DEEPSEEK_V3"
                "NOVITA_LLAMA_3_3_70B"
                "NOVITA_LLAMA_3_2_1B"
                "NOVITA_LLAMA_3_2_3B"
                "NOVITA_LLAMA_3_2_11B_VISION"
                "NOVITA_LLAMA_3_1_8B"
                "NOVITA_LLAMA_3_1_70B"
                "NOVITA_LLAMA_3_1_405B"
                "NOVITA_LLAMA_3_8B"
                "NOVITA_LLAMA_3_70B"
            )
        fi
    else
        update_or_add_env_var "ENABLE_NOVITA" "false"
    fi

    # OpenAI Compatible Configuration
    echo "To enable an OpenAI-compatible provider, you must have a model name, API key, and API base URL."
    read -p "Do you want to enable an OpenAI-compatible provider (y/n)? " enable_openai_compatible
    if [[ "$enable_openai_compatible" == "y" ]]; then
        read -p "Enter the model name (e.g., 'yi-34b', 'mistral-large'): " openai_compatible_model_name
        read -p "Enter your API key: " openai_compatible_api_key
        read -p "Enter the API base URL (e.g., 'https://api.together.xyz/v1'): " openai_compatible_api_base
        read -p "Does this model support vision (y/n)? " openai_compatible_vision
        
        if [ -z "$openai_compatible_model_name" ] || [ -z "$openai_compatible_api_key" ] || [ -z "$openai_compatible_api_base" ]; then
            echo "Error: All required fields must be populated."
            echo "OpenAI-compatible provider will not be enabled."
        else
            update_or_add_env_var "OPENAI_COMPATIBLE_MODEL_NAME" "$openai_compatible_model_name"
            update_or_add_env_var "OPENAI_COMPATIBLE_API_KEY" "$openai_compatible_api_key"
            update_or_add_env_var "OPENAI_COMPATIBLE_API_BASE" "$openai_compatible_api_base"
            
            # Set vision support
            if [[ "$openai_compatible_vision" == "y" ]]; then
                update_or_add_env_var "OPENAI_COMPATIBLE_SUPPORTS_VISION" "true"
            else
                update_or_add_env_var "OPENAI_COMPATIBLE_SUPPORTS_VISION" "false"
            fi

            update_or_add_env_var "ENABLE_OPENAI_COMPATIBLE" "true"
            model_options+=(
                "OPENAI_COMPATIBLE"
            )
        fi
    else
        update_or_add_env_var "ENABLE_OPENAI_COMPATIBLE" "false"
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
        update_or_add_env_var "LLM_KEY" "$chosen_model"
    fi

    echo "LLM provider configurations updated in .env."
}

# Function to initialize .env file
initialize_env_file() {
    if [ -f ".env" ]; then
        echo ".env file already exists, skipping initialization."
        read -p "Do you want to go through LLM provider setup again (y/n)? " redo_llm_setup
        if [[ "$redo_llm_setup" == "y" ]]; then
            setup_llm_providers
        fi
        return
    fi

    echo "Initializing .env file..."
    cp .env.example .env
    setup_llm_providers
    fix_database_string
    echo ".env file has been initialized."
}

initialize_frontend_env_file() {
    if [ -f "skyvern-frontend/.env" ]; then
        echo "skyvern-frontend/.env file already exists, skipping initialization."
        return
    fi

    echo "Initializing skyvern-frontend/.env file..."
    cp skyvern-frontend/.env.example skyvern-frontend/.env
    echo "skyvern-frontend/.env file has been initialized."
}

# Choose python version
choose_python_version_or_fail() {
  poetry env use python3.11
}

# Function to install dependencies
install_dependencies() {
    poetry install
    echo "Installing frontend dependencies"
    cd skyvern-frontend
    npm ci --silent
    cd ..
    echo "Frontend dependencies installed."
}

activate_poetry_env() {
    echo "Active poetry env"
    source "$(poetry env info --path)/bin/activate"
}

install_dependencies_after_poetry_env() {
    echo "Installing playwright dependencies..."
    playwright install
}

# Function to run Alembic upgrade
run_alembic_upgrade() {
    echo "Running Alembic upgrade..."
    poetry run alembic upgrade head
}

# Function to create organization and API token
create_organization() {
    echo "Creating organization and API token..."
    local org_output api_token
    org_output=$(poetry run python scripts/create_organization.py Skyvern-Open-Source)
    api_token=$(echo "$org_output" | awk '/token=/{gsub(/.*token='\''|'\''.*/, ""); print}')

    # Ensure .streamlit directory exists
    mkdir -p .streamlit

    # Check if secrets.toml exists and back it up
    if [ -f ".streamlit/secrets.toml" ]; then
        mv .streamlit/secrets.toml .streamlit/secrets.backup.toml
        echo "Existing secrets.toml file backed up as secrets.backup.toml"
    fi

    # Update the secrets-open-source.toml file
    echo -e "[skyvern]\nconfigs = [\n    {\"env\" = \"local\", \"host\" = \"http://127.0.0.1:8000/api/v1\", \"orgs\" = [{name=\"Skyvern\", cred=\"$api_token\"}]}\n]" > .streamlit/secrets.toml
    echo ".streamlit/secrets.toml file updated with organization details."

    # Check if skyvern-frontend/.env exists and back it up
    if [ -f "skyvern-frontend/.env" ]; then
        mv skyvern-frontend/.env skyvern-frontend/.env.backup
        echo "Existing skyvern-frontend/.env file backed up as skyvern-frontend/.env.backup"
        cp skyvern-frontend/.env.example skyvern-frontend/.env
    fi

    # Update the skyvern-frontend/.env file
    sed -i".old" -e "s/YOUR_API_KEY/$api_token/g" skyvern-frontend/.env
    echo "skyvern-frontend/.env file updated with API token."
}

main() {
    ensure_required_commands
    initialize_env_file
    initialize_frontend_env_file
    choose_python_version_or_fail
    install_dependencies
    activate_poetry_env
    install_dependencies_after_poetry_env
    run_alembic_upgrade
    create_organization
    echo "Setup completed successfully."
}

main