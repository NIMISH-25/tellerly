"""tellerly — command-line entry point."""
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from tellerly import __version__
from tellerly.config import load_settings

app = typer.Typer(
    help=(
        "Computer-use automation for legacy back-office apps: an LLM discovers a "
        "flow once, a typed capability artifact replays it deterministically."
    ),
    no_args_is_help=True,
)
caps_app = typer.Typer(help="Inspect the saved capability catalog.", no_args_is_help=True)
app.add_typer(caps_app, name="capabilities")
console = Console()
error_console = Console(stderr=True, style="bold red")


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
    job: Path = typer.Option(..., "--job", help="Job spec JSON (contract + this run's values)."),
    goal: Optional[str] = typer.Argument(None, help="Override the job's natural-language goal."),
    target: Optional[str] = typer.Option(None, "--target", help="Target base URL (default: TELLERLY_TARGET_URL)."),
    headed: bool = typer.Option(False, "--headed", help="Show the browser while the agent works."),
    max_turns: int = typer.Option(40, help="Planner turn budget."),
    throttle: float = typer.Option(
        4.0,
        help="Seconds between model calls. 4 fits flash-lite's 15/min free tier; "
        "use 12 for full Flash models (5/min).",
    ),
) -> None:
    """Run an LLM discovery run and compile the result into a capability."""
    from tellerly.discovery import DiscoveryEngine, GeminiPlanner, JobSpec
    from tellerly.kernel.guardrails import DeploymentPolicy, PolicyGate
    from tellerly.kernel.store import CapabilityStore
    from tellerly.schema import DiscoveryStatus
    from tellerly.surface.web import PlaywrightWebSurface

    settings = load_settings()
    if not settings.google_api_key:
        error_console.print("GOOGLE_API_KEY is not set — discovery needs the Gemini API. See .env.example.")
        raise typer.Exit(3)

    spec = JobSpec.load(job)
    if goal:
        spec = spec.model_copy(update={"goal": goal})

    policy = DeploymentPolicy.from_yaml(settings.repo_root / "config" / "policy.yaml")
    planner = GeminiPlanner(
        api_key=settings.google_api_key, model=settings.gemini_model, throttle_s=throttle
    )
    surface = PlaywrightWebSurface(headless=not headed)
    engine = DiscoveryEngine(
        surface=surface,
        planner=planner,
        job=spec,
        gate=PolicyGate(policy),
        base_url=target or settings.target_base_url,
        evidence_root=settings.evidence_dir,
        store=CapabilityStore(settings.capabilities_dir),
        outcome_catalog_path=settings.repo_root / "config" / "outcomes" / f"{spec.app_id}.json",
        max_turns=max_turns,
    )
    console.print(f"[dim]run {engine.run_id} — model {settings.gemini_model}[/dim]")
    try:
        result = engine.run()
    finally:
        surface.close()

    economics = result.economics
    table = Table(title=f"discovery: {result.status.value}", title_style="bold")
    table.add_column("field")
    table.add_column("value")
    table.add_row("goal", result.goal)
    table.add_row("steps recorded", str(result.steps_taken))
    table.add_row("artifact", result.artifact_path or "—")
    table.add_row("model calls", str(economics.llm_calls))
    table.add_row("tokens in / out", f"{economics.input_tokens} / {economics.output_tokens}")
    table.add_row("cost (list price)", f"${economics.cost_usd:.4f}")
    table.add_row("wall time", f"{economics.wall_time_s:.1f}s")
    table.add_row("evidence", result.evidence_dir or "—")
    console.print(table)

    raise typer.Exit(0 if result.status is DiscoveryStatus.GOAL_MET else 4)


@app.command()
def replay(
    capability: str = typer.Argument(..., help="Capability id to replay."),
) -> None:
    """Deterministically replay a saved capability. [not built yet]"""
    console.print(
        Panel(
            f"Capability: {capability}\n\n"
            "The replay engine is the next build; discovery and the capability "
            "catalog are live.",
            title="replay — not built yet",
        )
    )
    raise typer.Exit(2)


@caps_app.command(name="list")
def caps_list() -> None:
    """List saved capabilities."""
    from tellerly.kernel.store import CapabilityStore

    settings = load_settings()
    capabilities = CapabilityStore(settings.capabilities_dir).list()
    if not capabilities:
        console.print(f"No capabilities in {settings.capabilities_dir} yet — run `tellerly discover`.")
        return
    table = Table(title="capability catalog", title_style="bold")
    for column in ("id", "version", "title", "inputs", "outputs", "steps", "risk"):
        table.add_column(column)
    for cap in capabilities:
        from tellerly.schema import ActStep, Risk

        risky = any(
            isinstance(s, ActStep) and s.risk is Risk.MUTATING for s in cap.steps
        )
        table.add_row(
            cap.id,
            cap.version,
            cap.title,
            ", ".join(cap.inputs) or "—",
            ", ".join(cap.outputs) or "—",
            str(len(cap.steps)),
            "mutating" if risky else "read-only",
        )
    console.print(table)


@caps_app.command(name="show")
def caps_show(
    capability_id: str = typer.Argument(...),
    version: Optional[str] = typer.Option(None, "--version"),
    as_json: bool = typer.Option(False, "--json", help="Print the raw artifact JSON."),
) -> None:
    """Show one capability — its contract and its steps."""
    from tellerly.kernel.store import CapabilityStore
    from tellerly.schema import ActStep, CheckpointStep, ReadStep

    settings = load_settings()
    try:
        cap = CapabilityStore(settings.capabilities_dir).load(capability_id, version)
    except FileNotFoundError as exc:
        error_console.print(str(exc))
        raise typer.Exit(1)

    if as_json:
        console.print_json(cap.to_json())
        return

    console.print(Panel(f"{cap.title}\n{cap.description}", title=f"{cap.id} v{cap.version}"))
    contract = Table(title="contract")
    contract.add_column("kind")
    contract.add_column("name")
    contract.add_column("detail")
    for name, decl in cap.inputs.items():
        detail = f"{decl.type.value}, {decl.sensitivity.value}"
        if decl.pattern:
            detail += f", pattern {decl.pattern}"
        contract.add_row("input", name, detail)
    for name, decl in cap.outputs.items():
        contract.add_row("output", name, decl.type.value)
    for outcome in cap.outcomes:
        contract.add_row("outcome", outcome.id, f"{outcome.code.value} -> {outcome.disposition.value}")
    console.print(contract)

    steps = Table(title="steps")
    steps.add_column("#")
    steps.add_column("step")
    steps.add_column("what")
    for index, step in enumerate(cap.steps, 1):
        if isinstance(step, ActStep):
            what = f"{step.action.value} {step.target.description if step.target else step.value}"
            if step.value and step.target:
                what += f" = {step.value}"
            if step.risk.value == "mutating":
                what += "  [MUTATING]"
        elif isinstance(step, CheckpointStep):
            what = f"checkpoint: {step.description}"
        elif isinstance(step, ReadStep):
            what = f"read {step.output} from {step.target.description}"
        steps.add_row(str(index), step.id, what)
    console.print(steps)
    console.print(f"needs surface features: {', '.join(sorted(f.value for f in cap.required_features()))}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
