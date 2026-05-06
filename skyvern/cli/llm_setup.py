import os

from dotenv import load_dotenv, set_key
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from skyvern.analytics import capture_setup_event
from skyvern.utils.env_paths import resolve_backend_env_path

from .console import console

DEFAULT_POSTGRES_DATABASE_STRING = "postgresql+psycopg://skyvern@localhost:5432/skyvern"


def update_or_add_env_var(key: str, value: str) -> None:
    """Update or add environment variable in .env file."""
    env_path = resolve_backend_env_path()
    if not env_path.exists():
        env_path.touch()
        defaults = {
            "ENV": "local",
            "ENABLE_OPENAI": "false",
            "OPENAI_API_KEY": "",
            "ENABLE_ANTHROPIC": "false",
            "ANTHROPIC_API_KEY": "",
            "ENABLE_AZURE": "false",
            "AZURE_DEPLOYMENT": "",
            "AZURE_API_KEY": "",
            "AZURE_API_BASE": "",
            "AZURE_API_VERSION": "",
            "ENABLE_AZURE_GPT4O_MINI": "false",
            "AZURE_GPT4O_MINI_DEPLOYMENT": "",
            "AZURE_GPT4O_MINI_API_KEY": "",
            "AZURE_GPT4O_MINI_API_BASE": "",
            "AZURE_GPT4O_MINI_API_VERSION": "",
            "ENABLE_GEMINI": "false",
            "GEMINI_API_KEY": "",
            "LLM_KEY": "",
            "SECONDARY_LLM_KEY": "",
            "BROWSER_TYPE": "chromium-headful",
            "MAX_SCRAPING_RETRIES": "0",
            "VIDEO_PATH": "./videos",
            "BROWSER_ACTION_TIMEOUT_MS": "5000",
            "MAX_STEPS_PER_RUN": "50",
            "LOG_LEVEL": "INFO",
            "LITELLM_LOG": "CRITICAL",
            "DATABASE_STRING": DEFAULT_POSTGRES_DATABASE_STRING,
            "PORT": "8000",
            "ANALYTICS_ID": "anonymous",
            "ENABLE_LOG_ARTIFACTS": "false",
        }
        for k, v in defaults.items():
            set_key(env_path, k, v)

    load_dotenv(env_path)
    set_key(env_path, key, value)
    os.environ[key] = value


def setup_llm_providers() -> None:
    """Configure Large Language Model (LLM) Providers."""
    console.print(Panel("[bold magenta]LLM Provider Configuration[/bold magenta]", border_style="purple"))
    console.print("[italic]Note: All information provided here will be stored only on your local machine.[/italic]")
    capture_setup_event("llm-start")
    model_options: list[str] = []
    enabled_providers: list[str] = []

    console.print("\n[bold blue]--- OpenAI Configuration ---[/bold blue]")
    console.print("To enable OpenAI, you must have an OpenAI API key.")
    enable_openai = Confirm.ask("Do you want to enable OpenAI?")
    if enable_openai:
        openai_api_key = Prompt.ask("Enter your OpenAI API key", password=True)
        if not openai_api_key:
            console.print("[red]Error: OpenAI API key is required. OpenAI will not be enabled.[/red]")
        else:
            update_or_add_env_var("OPENAI_API_KEY", openai_api_key)
            update_or_add_env_var("ENABLE_OPENAI", "true")
            enabled_providers.append("openai")
            model_options.extend(
                [
                    "OPENAI_GPT5_5",
                    "OPENAI_GPT5_4",
                    "OPENAI_GPT5",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_OPENAI", "false")

    console.print("\n[bold blue]--- Anthropic Configuration ---[/bold blue]")
    console.print("To enable Anthropic, you must have an Anthropic API key.")
    enable_anthropic = Confirm.ask("Do you want to enable Anthropic?")
    if enable_anthropic:
        anthropic_api_key = Prompt.ask("Enter your Anthropic API key", password=True)
        if not anthropic_api_key:
            console.print("[red]Error: Anthropic API key is required. Anthropic will not be enabled.[/red]")
        else:
            update_or_add_env_var("ANTHROPIC_API_KEY", anthropic_api_key)
            update_or_add_env_var("ENABLE_ANTHROPIC", "true")
            enabled_providers.append("anthropic")
            model_options.extend(
                [
                    "ANTHROPIC_CLAUDE4.7_OPUS",
                    "ANTHROPIC_CLAUDE4.6_OPUS",
                    "ANTHROPIC_CLAUDE4.6_SONNET",
                    "ANTHROPIC_CLAUDE4.5_SONNET",
                    "ANTHROPIC_CLAUDE4.5_HAIKU",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_ANTHROPIC", "false")

    console.print("\n[bold blue]--- Azure Configuration ---[/bold blue]")
    console.print("To enable Azure, you must have an Azure deployment name, API key, base URL, and API version.")
    enable_azure = Confirm.ask("Do you want to enable Azure?")
    if enable_azure:
        azure_deployment = Prompt.ask("Enter your Azure deployment name")
        azure_api_key = Prompt.ask("Enter your Azure API key", password=True)
        azure_api_base = Prompt.ask("Enter your Azure API base URL")
        azure_api_version = Prompt.ask("Enter your Azure API version")
        if not all([azure_deployment, azure_api_key, azure_api_base, azure_api_version]):
            console.print("[red]Error: All Azure fields must be populated. Azure will not be enabled.[/red]")
        else:
            update_or_add_env_var("AZURE_DEPLOYMENT", azure_deployment)
            update_or_add_env_var("AZURE_API_KEY", azure_api_key)
            update_or_add_env_var("AZURE_API_BASE", azure_api_base)
            update_or_add_env_var("AZURE_API_VERSION", azure_api_version)
            update_or_add_env_var("ENABLE_AZURE", "true")
            enabled_providers.append("azure")
            model_options.append("AZURE_OPENAI")
    else:
        update_or_add_env_var("ENABLE_AZURE", "false")

    console.print("\n[bold blue]--- Gemini Configuration ---[/bold blue]")
    console.print("To enable Gemini, you must have a Gemini API key.")
    enable_gemini = Confirm.ask("Do you want to enable Gemini?")
    if enable_gemini:
        gemini_api_key = Prompt.ask("Enter your Gemini API key", password=True)
        if not gemini_api_key:
            console.print("[red]Error: Gemini API key is required. Gemini will not be enabled.[/red]")
        else:
            update_or_add_env_var("GEMINI_API_KEY", gemini_api_key)
            update_or_add_env_var("ENABLE_GEMINI", "true")
            enabled_providers.append("gemini")
            model_options.extend(
                [
                    "GEMINI_3.1_PRO",
                    "GEMINI_3.0_FLASH",
                    "GEMINI_3.1_FLASH_LITE",
                    "GEMINI_2.5_PRO",
                    "GEMINI_2.5_FLASH",
                    "GEMINI_2.5_FLASH_LITE",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_GEMINI", "false")

    console.print("\n[bold blue]--- Ollama / Local LLM Configuration ---[/bold blue]")
    console.print("Use any locally-running model via Ollama (e.g. gemma4, qwen3, deepseek-r1).")
    console.print("[dim]Requires Ollama running locally: https://ollama.com[/dim]")
    enable_ollama = Confirm.ask("Do you want to enable a local Ollama model?")
    if enable_ollama:
        ollama_server_url = Prompt.ask(
            "Enter Ollama server URL",
            default="http://localhost:11434",
        )
        ollama_model = Prompt.ask(
            "Enter model name (e.g. gemma4, qwen3, deepseek-r1)",
        )
        if not ollama_model:
            console.print("[red]Error: Model name is required. Ollama will not be enabled.[/red]")
        else:
            ollama_vision = Confirm.ask("Does this model support vision?", default=False)
            update_or_add_env_var("OLLAMA_SERVER_URL", ollama_server_url)
            update_or_add_env_var("OLLAMA_MODEL", ollama_model)
            update_or_add_env_var("OLLAMA_SUPPORTS_VISION", str(ollama_vision).lower())
            update_or_add_env_var("ENABLE_OLLAMA", "true")
            enabled_providers.append("ollama")
            model_options.append("OLLAMA")
    else:
        update_or_add_env_var("ENABLE_OLLAMA", "false")

    if not model_options:
        capture_setup_event(
            "llm-no-provider",
            success=False,
            error_type="no_provider_enabled",
            error_message="No LLM providers were enabled during setup",
        )
        console.print(
            Panel(
                "[bold red]No LLM providers enabled.[/bold red]\n"
                "You won't be able to run Skyvern unless you enable at least one provider.\n"
                "You can re-run this script to enable providers or manually update the .env file.",
                border_style="red",
            )
        )
    else:
        console.print("\n[bold green]Available LLM models based on your selections:[/bold green]")
        for i, model in enumerate(model_options, 1):
            console.print(f"  [cyan]{i}.[/cyan] [green]{model}[/green]")

        chosen_model_idx = Prompt.ask(
            f"Choose a model by number (e.g., [cyan]1[/cyan] for [green]{model_options[0]}[/green])",
            choices=[str(i) for i in range(1, len(model_options) + 1)],
            default="1",
        )
        chosen_model = model_options[int(chosen_model_idx) - 1]
        console.print(f"🎉 [bold green]Chosen LLM Model: {chosen_model}[/bold green]")
        update_or_add_env_var("LLM_KEY", chosen_model)
        capture_setup_event(
            "llm-complete",
            success=True,
            extra_data={
                "enabled_providers": enabled_providers,
                "chosen_model": chosen_model,
                "provider_count": len(enabled_providers),
            },
        )

    console.print("✅ [green]LLM provider configurations updated in .env.[/green]")
