"""CLI entrypoint for the BGG research agent."""

import os
import sys

from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.text import Text

from bgg_agentcore import BggAgent

load_dotenv()

console = Console()

_WELCOME = """
# BGG Research Agent

Ask me anything about board games! I can:
- Search for games by name or keyword
- Compare games side by side
- Show current trending games on BGG
- Recommend games based on your preferences

**Commands:** `reset` — clear conversation history | `quit` / `exit` — quit
"""

_COMMANDS = {"reset", "quit", "exit", "q"}


def _print_welcome() -> None:
    console.print(Panel(Markdown(_WELCOME), border_style="bold blue", padding=(1, 2)))


def _print_response(text: str) -> None:
    console.print(Panel(Markdown(text), border_style="green", padding=(1, 2)))


def _print_error(msg: str) -> None:
    console.print(f"[bold red]Error:[/bold red] {msg}")


def run() -> None:
    region = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
    if not region:
        _print_error(
            "No AWS region configured. The agent now calls Claude through Amazon "
            "Bedrock — run `aws configure` or set AWS_REGION in your .env."
        )
        sys.exit(1)

    _print_welcome()

    with BggAgent(region=region) as agent:
        while True:
            try:
                user_input = Prompt.ask(
                    Text("\nYou", style="bold cyan")
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Goodbye![/dim]")
                break

            if not user_input:
                continue

            if user_input.lower() in _COMMANDS:
                if user_input.lower() == "reset":
                    agent.reset()
                    console.print("[dim]Conversation history cleared.[/dim]")
                else:
                    console.print("[dim]Goodbye![/dim]")
                    break
                continue

            with console.status("[bold green]Thinking...[/bold green]", spinner="dots"):
                try:
                    reply = agent.chat(user_input)
                except Exception as exc:
                    _print_error(str(exc))
                    continue

            _print_response(reply)


if __name__ == "__main__":
    run()
