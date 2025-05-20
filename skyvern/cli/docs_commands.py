import webbrowser
import typer
from rich.markdown import Markdown
from rich.panel import Panel

from .common import console

DOCUMENTATION = {
    "quickstart": "https://docs.skyvern.com/introduction",
    "tasks": "https://docs.skyvern.com/running-tasks/introduction",
    "workflows": "https://docs.skyvern.com/workflows/introduction",
    "prompting": "https://docs.skyvern.com/getting-started/prompting-guide",
    "api": "https://docs.skyvern.com/integrations/api",
}


def open_docs(section: str = typer.Argument("quickstart", help="Documentation section to open")) -> None:
    """Open Skyvern documentation in your web browser."""
    if section not in DOCUMENTATION:
        console.print(f"[bold red]Error:[/] Documentation section '{section}' not found")
        console.print("\nAvailable sections:")
        for name, url in DOCUMENTATION.items():
            console.print(f"  • [bold]{name}[/] - {url}")
        return

    url = DOCUMENTATION[section]
    console.print(f"Opening documentation section: [bold]{section}[/]")
    console.print(f"URL: [link={url}]{url}[/link]")
    webbrowser.open(url)


def prompting_guide() -> None:
    """Show prompting best practices for Skyvern."""
    console.print(
        Panel.fit("[bold blue]Skyvern Prompting Best Practices[/]", subtitle="Tips for writing effective prompts")
    )
    console.print(
        Markdown(
            """
## General Guidelines

1. **Be specific and detailed**
   - Specify exactly what actions should be taken
   - Include any data or criteria needed for decisions

2. **Define completion criteria**
   - Use COMPLETE/TERMINATE markers to indicate success/failure conditions
   - Specify what data to extract (if any)

3. **Break complex tasks into steps**
   - For multi-page flows, describe each step clearly
   - Use sequencing terms (first, then, after)

## Examples

✅ **Good prompt:**
```
Navigate to the products page. Find the product named "Wireless Headphones" and add it to the cart. Proceed to checkout and fill the form with:
Name: John Doe
Email: john@example.com
When complete, extract the order confirmation number.
COMPLETE when you see a "Thank you for your order" message.
```

❌ **Less effective prompt:**
```
Buy wireless headphones and check out.
```

## For More Information

Run `skyvern docs open prompting` to see the complete prompting guide online.
            """
        )
    )

