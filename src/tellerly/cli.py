"""tellerly — command-line entry point.

Working today: ``start-app`` (the mock legacy console) and ``version``.
``discover`` / ``replay`` / ``capabilities`` are the seams the later phases
fill in; they explain themselves and exit non-zero rather than pretending.
"""
import sys
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel

from tellerly import __version__
from tellerly.config import load_settings

app = typer.Typer(
    help=(
        "Computer-use automation for legacy back-office apps: an LLM discovers a "
        "flow once, a typed capability artifact replays it deterministically."
    ),
    no_args_is_help=True,
)
console = Console()

_NOT_BUILT_EXIT = 2


@app.command()
def version() -> None:
    """Print the tellerly version."""
    console.print(f"tellerly {__version__}")


@app.command(name="start-app")
def start_app(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
    interstitial_every: int = typer.Option(
        3, help="Show the maintenance interstitial every Nth member-record load (0 disables)."
    ),
    session_ttl: int = typer.Option(180, help="Idle seconds before the operator session expires."),
) -> None:
    """Run the mock target: the Tellerly Teller Console (fictional legacy app)."""
    settings = load_settings()
    sys.path.insert(0, str(settings.repo_root))
    from target_app.app import create_app

    console.print(
        Panel(
            f"Tellerly Teller Console (mock target)\n"
            f"http://{host}:{port}  —  sign in with any operator ID, access key 'demo'\n"
            f"interstitial: every {interstitial_every or 'never'} member-record load(s), "
            f"session TTL: {session_ttl}s",
            title="target app",
        )
    )
    create_app(
        {"INTERSTITIAL_EVERY": interstitial_every, "SESSION_TTL_S": session_ttl}
    ).run(host=host, port=port, use_reloader=False)


@app.command()
def discover(
    goal: str = typer.Argument(..., help='e.g. "transfer $25 from member 101555\'s savings to checking"'),
    target: Optional[str] = typer.Option(None, "--target", help="Entry URL (default: TELLERLY_TARGET_URL)."),
) -> None:
    """Run an LLM discovery run against the target surface. [Phase 2.2 — not built yet]"""
    settings = load_settings()
    console.print(
        Panel(
            f"Goal: {goal}\nTarget: {target or settings.target_base_url}\n\n"
            "The planner loop is Phase 2.2 and is designed against the Phase 1 "
            "schemas, which come first. See HANDOFF.md.",
            title="discover — not built yet",
        )
    )
    raise typer.Exit(_NOT_BUILT_EXIT)


@app.command()
def replay(
    capability: str = typer.Argument(..., help="Capability id to replay."),
) -> None:
    """Deterministically replay a saved capability. [Phase 2.4 — not built yet]"""
    console.print(
        Panel(
            f"Capability: {capability}\n\n"
            "The replay engine is Phase 2.4; the artifact schema it executes is "
            "designed in Phase 1.1. See HANDOFF.md.",
            title="replay — not built yet",
        )
    )
    raise typer.Exit(_NOT_BUILT_EXIT)


@app.command()
def capabilities() -> None:
    """List saved capability artifacts. [lands with the Phase 1.1 schema]"""
    settings = load_settings()
    console.print(
        Panel(
            f"Catalog directory: {settings.capabilities_dir}\n\n"
            "Empty until the Phase 1.1 artifact schema and Phase 2.3 compiler land.",
            title="capabilities — not built yet",
        )
    )
    raise typer.Exit(_NOT_BUILT_EXIT)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
