import os

from dotenv import load_dotenv, set_key
from rich.panel import Panel
from rich.prompt import Confirm, Prompt

from skyvern.utils.env_paths import resolve_backend_env_path

from .console import console


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
            "ENABLE_NOVITA": "false",
            "NOVITA_API_KEY": "",
            "LLM_KEY": "",
            "SECONDARY_LLM_KEY": "",
            "BROWSER_TYPE": "chromium-headful",
            "MAX_SCRAPING_RETRIES": "0",
            "VIDEO_PATH": "./videos",
            "BROWSER_ACTION_TIMEOUT_MS": "5000",
            "MAX_STEPS_PER_RUN": "50",
            "LOG_LEVEL": "INFO",
            "LITELLM_LOG": "CRITICAL",
            "DATABASE_STRING": "postgresql+psycopg://skyvern@localhost/skyvern",
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
    model_options: list[str] = []

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
            model_options.extend(
                [
                    "OPENAI_GPT4_1",
                    "OPENAI_GPT4_1_MINI",
                    "OPENAI_GPT4_1_NANO",
                    "OPENAI_GPT4O",
                    "OPENAI_O4_MINI",
                    "OPENAI_O3",
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
            model_options.extend(
                [
                    "ANTHROPIC_CLAUDE3.5_SONNET",
                    "ANTHROPIC_CLAUDE3.7_SONNET",
                    "ANTHROPIC_CLAUDE3.5_HAIKU",
                    "ANTHROPIC_CLAUDE4_OPUS",
                    "ANTHROPIC_CLAUDE4_SONNET",
                    "ANTHROPIC_CLAUDE4.5_HAIKU",
                    "ANTHROPIC_CLAUDE4.5_SONNET",
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
    else:
        update_or_add_env_var("ENABLE_GEMINI", "false")

    console.print("\n[bold blue]--- Novita Configuration ---[/bold blue]")
    console.print("To enable Novita, you must have a Novita API key.")
    enable_novita = Confirm.ask("Do you want to enable Novita?")
    if enable_novita:
        novita_api_key = Prompt.ask("Enter your Novita API key", password=True)
        if not novita_api_key:
            console.print("[red]Error: Novita API key is required. Novita will not be enabled.[/red]")
        else:
            update_or_add_env_var("NOVITA_API_KEY", novita_api_key)
            update_or_add_env_var("ENABLE_NOVITA", "true")
            model_options.extend(
                [
                    "NOVITA_LLAMA_3_2_11B_VISION",
                    "NOVITA_LLAMA_3_1_8B",
                    "NOVITA_LLAMA_3_1_70B",
                    "NOVITA_LLAMA_3_1_405B",
                    "NOVITA_LLAMA_3_8B",
                    "NOVITA_LLAMA_3_70B",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_NOVITA", "false")

    console.print("\n[bold blue]--- VolcEngine Configuration ---[/bold blue]")
    console.print("To enable VolcEngine, you must have a ByteDance Doubao API key.")
    enable_volcengine = Confirm.ask("Do you want to enable VolcEngine?")
    if enable_volcengine:
        volcengine_api_key = Prompt.ask("Enter your VolcEngine(ByteDance Doubao) API key", password=True)
        if not volcengine_api_key:
            console.print("[red]Error: VolcEngine key is required. VolcEngine will not be enabled.[/red]")
        else:
            update_or_add_env_var("VOLCENGINE_API_KEY", volcengine_api_key)
            update_or_add_env_var("ENABLE_VOLCENGINE", "true")

            model_options.extend(
                [
                    "VOLCENGINE_DOUBAO_SEED_1_6",
                    "VOLCENGINE_DOUBAO_SEED_1_6_FLASH",
                    "VOLCENGINE_DOUBAO_1_5_THINKING_VISION_PRO",
                ]
            )
    else:
        update_or_add_env_var("ENABLE_VOLCENGINE", "false")

    console.print("\n[bold blue]--- OpenAI-Compatible Provider Configuration ---[/bold blue]")
    console.print("To enable an OpenAI-compatible provider, you must have a model name, API key, and API base URL.")
    enable_openai_compatible = Confirm.ask("Do you want to enable an OpenAI-compatible provider?")
    if enable_openai_compatible:
        openai_compatible_model_name = Prompt.ask("Enter the model name (e.g., 'yi-34b', 'mistral-large')")
        openai_compatible_api_key = Prompt.ask("Enter your API key", password=True)
        openai_compatible_api_base = Prompt.ask("Enter the API base URL (e.g., 'https://api.together.xyz/v1')")
        openai_compatible_vision = Confirm.ask("Does this model support vision?")

        if not all([openai_compatible_model_name, openai_compatible_api_key, openai_compatible_api_base]):
            console.print(
                "[red]Error: All required fields must be populated. OpenAI-compatible provider will not be enabled.[/red]"
            )
        else:
            update_or_add_env_var("OPENAI_COMPATIBLE_MODEL_NAME", openai_compatible_model_name)
            update_or_add_env_var("OPENAI_COMPATIBLE_API_KEY", openai_compatible_api_key)
            update_or_add_env_var("OPENAI_COMPATIBLE_API_BASE", openai_compatible_api_base)
            if openai_compatible_vision:
                update_or_add_env_var("OPENAI_COMPATIBLE_SUPPORTS_VISION", "true")
            else:
                update_or_add_env_var("OPENAI_COMPATIBLE_SUPPORTS_VISION", "false")

            openai_compatible_api_version = Prompt.ask("Enter API version (optional, press enter to skip)", default="")
            if openai_compatible_api_version:
                update_or_add_env_var("OPENAI_COMPATIBLE_API_VERSION", openai_compatible_api_version)

            update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "true")
            model_options.append("OPENAI_COMPATIBLE")
    else:
        update_or_add_env_var("ENABLE_OPENAI_COMPATIBLE", "false")

    if not model_options:
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

    console.print("✅ [green]LLM provider configurations updated in .env.[/green]")
