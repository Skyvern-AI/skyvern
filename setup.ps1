# PowerShell script for Windows setup

# Function to check if a command exists
function CommandExists {
    param (
        [string]$command
    )
    $commandPath = Get-Command $command -ErrorAction SilentlyContinue
    return $commandPath -ne $null
}

# Function to update or add environment variable in .env file
function UpdateOrAddEnvVar {
    param (
        [string]$key,
        [string]$value
    )
    $envFilePath = ".env"
    if (Test-Path $envFilePath) {
        $envContent = Get-Content -Raw $envFilePath
        $envContent = $envContent -replace "^$key=.*", "$key=$value"
        if ($envContent -notmatch "^$key=") {
            Add-Content $envFilePath "$key=$value"
        } else {
            Set-Content $envFilePath $envContent
        }
    } else {
        Add-Content $envFilePath "$key=$value"
    }
}

# Function to set up LLM provider environment variables
function SetupLLMProviders {
    Write-Host "Configuring Large Language Model (LLM) Providers..."
    Write-Host "Note: All information provided here will be stored only on your local machine."
    $modelOptions = @()

    # OpenAI Configuration
    Write-Host "To enable OpenAI, you must have an OpenAI API key."
    $enableOpenAI = Read-Host "Do you want to enable OpenAI (y/n)?"
    if ($enableOpenAI -eq "y") {
        $openaiApiKey = Read-Host "Enter your OpenAI API key"
        if ($openaiApiKey) {
            UpdateOrAddEnvVar "OPENAI_API_KEY" $openaiApiKey
            UpdateOrAddEnvVar "ENABLE_OPENAI" "true"
            $modelOptions += "OPENAI_GPT4_TURBO", "OPENAI_GPT4V", "OPENAI_GPT4O"
        } else {
            Write-Host "Error: OpenAI API key is required."
            Write-Host "OpenAI will not be enabled."
        }
    } else {
        UpdateOrAddEnvVar "ENABLE_OPENAI" "false"
    }

    # Anthropic Configuration
    Write-Host "To enable Anthropic, you must have an Anthropic API key."
    $enableAnthropic = Read-Host "Do you want to enable Anthropic (y/n)?"
    if ($enableAnthropic -eq "y") {
        $anthropicApiKey = Read-Host "Enter your Anthropic API key"
        if ($anthropicApiKey) {
            UpdateOrAddEnvVar "ANTHROPIC_API_KEY" $anthropicApiKey
            UpdateOrAddEnvVar "ENABLE_ANTHROPIC" "true"
            $modelOptions += "ANTHROPIC_CLAUDE3_OPUS", "ANTHROPIC_CLAUDE3_SONNET", "ANTHROPIC_CLAUDE3_HAIKU", "ANTHROPIC_CLAUDE3.5_SONNET"
        } else {
            Write-Host "Error: Anthropic API key is required."
            Write-Host "Anthropic will not be enabled."
        }
    } else {
        UpdateOrAddEnvVar "ENABLE_ANTHROPIC" "false"
    }

    # Azure Configuration
    Write-Host "To enable Azure, you must have an Azure deployment name, API key, base URL, and API version."
    $enableAzure = Read-Host "Do you want to enable Azure (y/n)?"
    if ($enableAzure -eq "y") {
        $azureDeployment = Read-Host "Enter your Azure deployment name"
        $azureApiKey = Read-Host "Enter your Azure API key"
        $azureApiBase = Read-Host "Enter your Azure API base URL"
        $azureApiVersion = Read-Host "Enter your Azure API version"
        if ($azureDeployment -and $azureApiKey -and $azureApiBase -and $azureApiVersion) {
            UpdateOrAddEnvVar "AZURE_DEPLOYMENT" $azureDeployment
            UpdateOrAddEnvVar "AZURE_API_KEY" $azureApiKey
            UpdateOrAddEnvVar "AZURE_API_BASE" $azureApiBase
            UpdateOrAddEnvVar "AZURE_API_VERSION" $azureApiVersion
            UpdateOrAddEnvVar "ENABLE_AZURE" "true"
            $modelOptions += "AZURE_OPENAI_GPT4V"
        } else {
            Write-Host "Error: All Azure fields must be populated."
            Write-Host "Azure will not be enabled."
        }
    } else {
        UpdateOrAddEnvVar "ENABLE_AZURE" "false"
    }

    # Gemini Configuration
    Write-Host "To enable Gemini, you must have a Gemini API key."
    $enableGemini = Read-Host "Do you want to enable Gemini (y/n)?"
    if ($enableGemini -eq "y") {
        $geminiApiKey = Read-Host "Enter your Gemini API key"
        if ($geminiApiKey) {
            UpdateOrAddEnvVar "GEMINI_API_KEY" $geminiApiKey
            UpdateOrAddEnvVar "ENABLE_GEMINI" "true"
            $modelOptions += "GEMINI_PRO"
        } else {
            Write-Host "Error: Gemini API key is required."
            Write-Host "Gemini will not be enabled."
        }
    } else {
        UpdateOrAddEnvVar "ENABLE_GEMINI" "false"
    }

    # Novita AI Configuration
    Write-Host "To enable Novita AI, you must have a Novita AI API key."
    $enableNovita = Read-Host "Do you want to enable Novita AI (y/n)?"
    if ($enableNovita -eq "y") {
        $novitaApiKey = Read-Host "Enter your Novita AI API key"
        if ($novitaApiKey) {
            UpdateOrAddEnvVar "NOVITA_API_KEY" $novitaApiKey
            UpdateOrAddEnvVar "ENABLE_NOVITA" "true"
            $modelOptions += "NOVITA_DEEPSEEK_R1", "NOVITA_DEEPSEEK_V3", "NOVITA_LLAMA_3_3_70B", "NOVITA_LLAMA_3_2_1B", "NOVITA_LLAMA_3_2_3B", "NOVITA_LLAMA_3_2_11B_VISION", "NOVITA_LLAMA_3_1_8B", "NOVITA_LLAMA_3_1_70B", "NOVITA_LLAMA_3_1_405B", "NOVITA_LLAMA_3_8B", "NOVITA_LLAMA_3_70B"
        } else {
            Write-Host "Error: Novita AI API key is required."
            Write-Host "Novita AI will not be enabled."
        }
    } else {
        UpdateOrAddEnvVar "ENABLE_NOVITA" "false"
    }

    # Model Selection
    if ($modelOptions.Count -eq 0) {
        Write-Host "No LLM providers enabled. You won't be able to run Skyvern unless you enable at least one provider. You can re-run this script to enable providers or manually update the .env file."
    } else {
        Write-Host "Available LLM models based on your selections:"
        for ($i = 0; $i -lt $modelOptions.Count; $i++) {
            Write-Host "$($i + 1). $($modelOptions[$i])"
        }
        $modelChoice = Read-Host "Choose a model by number (e.g., 1 for $($modelOptions[0]))"
        $chosenModel = $modelOptions[$modelChoice - 1]
        Write-Host "Chosen LLM Model: $chosenModel"
        UpdateOrAddEnvVar "LLM_KEY" $chosenModel
    }

    Write-Host "LLM provider configurations updated in .env."
}

# Function to initialize .env file
function InitializeEnvFile {
    if (Test-Path ".env") {
        Write-Host ".env file already exists, skipping initialization."
        $redoLLMSetup = Read-Host "Do you want to go through LLM provider setup again (y/n)?"
        if ($redoLLMSetup -eq "y") {
            SetupLLMProviders
        }
        return
    }

    Write-Host "Initializing .env file..."
    Copy-Item ".env.example" ".env"
    SetupLLMProviders

    # Ask for email or generate UUID
    $analyticsId = Read-Host "Please enter your email for analytics (press enter to skip)"
    if (-not $analyticsId) {
        $analyticsId = [guid]::NewGuid().ToString()
    }
    UpdateOrAddEnvVar "ANALYTICS_ID" $analyticsId
    Write-Host ".env file has been initialized."
}

# Function to initialize skyvern-frontend/.env file
function InitializeFrontendEnvFile {
    if (Test-Path "skyvern-frontend/.env") {
        Write-Host "skyvern-frontend/.env file already exists, skipping initialization."
        return
    }

    Write-Host "Initializing skyvern-frontend/.env file..."
    Copy-Item "skyvern-frontend/.env.example" "skyvern-frontend/.env"
    Write-Host "skyvern-frontend/.env file has been initialized."
}

# Function to install dependencies
function InstallDependencies {
    Write-Host "Installing dependencies..."
    poetry install
    Write-Host "Installing frontend dependencies..."
    Push-Location "skyvern-frontend"
    npm install --silent
    Pop-Location
    Write-Host "Dependencies installed."
}

# Function to set up PostgreSQL
function SetupPostgreSQL {
    Write-Host "Setting up PostgreSQL..."
    if (CommandExists "psql") {
        if (pg_isready) {
            Write-Host "PostgreSQL is already running locally."
            if (psql skyvern -U skyvern -c '\q') {
                Write-Host "Connection successful. Database and user exist."
            } else {
                createuser skyvern
                createdb skyvern -O skyvern
                Write-Host "Database and user created successfully."
            }
            return
        }
    }

    if (-not (CommandExists "docker") -or -not (docker info > $null 2>&1)) {
        Write-Host "Docker is not running or not installed. Please install or start Docker and try again."
        exit 1
    }

    if (docker ps | Select-String -Pattern "postgresql-container") {
        Write-Host "PostgreSQL is already running in a Docker container."
    } else {
        Write-Host "Attempting to install PostgreSQL via Docker..."
        docker run --name postgresql-container -e POSTGRES_HOST_AUTH_METHOD=trust -d -p 5432:5432 postgres:14
        Write-Host "PostgreSQL has been installed and started using Docker."
        Start-Sleep -Seconds 20
    }

    if (docker exec postgresql-container psql -U postgres -c "\du" | Select-String -Pattern "skyvern") {
        Write-Host "Database user exists."
    } else {
        Write-Host "Creating database user..."
        docker exec postgresql-container createuser -U postgres skyvern
    }

    if (docker exec postgresql-container psql -U postgres -lqt | Select-String -Pattern "skyvern") {
        Write-Host "Database exists."
    } else {
        Write-Host "Creating database..."
        docker exec postgresql-container createdb -U postgres skyvern -O skyvern
        Write-Host "Database and user created successfully."
    }
}

# Function to run Alembic upgrade
function RunAlembicUpgrade {
    Write-Host "Running Alembic upgrade..."
    alembic upgrade head
}

# Function to create organization and API token
function CreateOrganization {
    Write-Host "Creating organization and API token..."
    $orgOutput = poetry run python scripts/create_organization.py Skyvern-Open-Source
    $apiToken = $orgOutput -match "token='([^']+)'" | Out-Null; $matches[1]

    if (-not (Test-Path ".streamlit")) {
        New-Item -ItemType Directory -Path ".streamlit" | Out-Null
    }

    if (Test-Path ".streamlit/secrets.toml") {
        Rename-Item ".streamlit/secrets.toml" ".streamlit/secrets.backup.toml"
        Write-Host "Existing secrets.toml file backed up as secrets.backup.toml"
    }

    $secretsContent = @"
[skyvern]
configs = [
    {"env" = "local", "host" = "http://127.0.0.1:8000/api/v1", "orgs" = [{name="Skyvern", cred="$apiToken"}]}
]
"@
    $secretsContent | Out-File -FilePath ".streamlit/secrets.toml" -Encoding utf8
    Write-Host ".streamlit/secrets.toml file updated with organization details."

    if (Test-Path "skyvern-frontend/.env") {
        Rename-Item "skyvern-frontend/.env" "skyvern-frontend/.env.backup"
        Write-Host "Existing skyvern-frontend/.env file backed up as skyvern-frontend/.env.backup"
        Copy-Item "skyvern-frontend/.env.example" "skyvern-frontend/.env"
    }

    (Get-Content "skyvern-frontend/.env") -replace "YOUR_API_KEY", $apiToken | Set-Content "skyvern-frontend/.env"
    Write-Host "skyvern-frontend/.env file updated with API token."
}

# Main function
function Main {
    InitializeEnvFile
    InitializeFrontendEnvFile
    InstallDependencies
    SetupPostgreSQL
    RunAlembicUpgrade
    CreateOrganization
    Write-Host "Setup completed successfully."
}

# Execute main function
Main
