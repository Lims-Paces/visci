"""
Command-line interface.

For week 1 this is the entire user interface. No web app, no database — you type
a premise, you get a film. Building the UI over an unproven pipeline would be
wasted work; this proves the pipeline first.

Usage:
    visci generate "a lighthouse keeper hears a ghost ship"
    visci generate "..." --mode narration
    visci generate "..." --real          # spends actual money
"""

import uuid
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel

from visci.config import settings
from visci.models import DEFAULT_FORMAT, Format, Mode
from visci.pipeline.run import Job
from visci.providers.base import Providers
from visci.providers.mock import (
    MockImageProvider,
    MockStoryProvider,
    MockVideoProvider,
    MockVoiceProvider,
)

app = typer.Typer(add_completion=False, help="Generate a 2-minute vertical film.")
console = Console()


def build_providers(
    story_real: bool, media_real: bool, draft: bool = False
) -> Providers:
    """
    Choose real or mock implementations, independently for story and media.

    Splitting these two is deliberate and useful. Story generation costs ~$0.60;
    video costs ~$15. So you can iterate on real scripts — reading what Claude
    actually writes, tuning the prompts — while video stays mocked and free.
    That is the cheapest possible way to get the writing right, and the writing
    is what determines the quality of everything downstream.
    """
    if story_real:
        from visci.providers.claude import ClaudeStoryProvider

        story = ClaudeStoryProvider()
    else:
        story = MockStoryProvider()

    if not media_real:
        return Providers(
            story=story,
            image=MockImageProvider(),
            video=MockVideoProvider(),
            voice=MockVoiceProvider(),
        )

    from visci.providers.gemini import (
        GeminiImageProvider,
        GeminiVoiceProvider,
        VeoVideoProvider,
    )

    return Providers(
        story=story,
        image=GeminiImageProvider(),
        video=VeoVideoProvider(draft=draft),
        voice=GeminiVoiceProvider(),
    )


@app.command()
def generate(
    premise: str = typer.Argument(..., help="Your movie idea, in a sentence or two."),
    mode: Mode = typer.Option(Mode.DIALOGUE, help="dialogue or narration"),
    fmt: Format = typer.Option(
        DEFAULT_FORMAT, "--format", help="short (30s) or film (2 min)"
    ),
    story_real: bool = typer.Option(
        False,
        "--story-real",
        help="Use the real Claude API for script generation (~$0.60/film).",
    ),
    real: bool = typer.Option(
        False,
        "--real",
        help="Use real APIs for everything, including video (~$16/film).",
    ),
    draft: bool = typer.Option(
        False,
        "--draft",
        help="With --real, use Veo Lite: cheaper video (~$10 total), lower quality.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the spend confirmation prompt."
    ),
    job_id: str = typer.Option(None, help="Resume an existing job by id."),
) -> None:
    """Generate a film from a premise."""

    if job_id is None:
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        job_id = f"{stamp}-{uuid.uuid4().hex[:6]}"

    # A resume must find real saved state. Without this check, resuming a job
    # whose state is missing silently generates a DIFFERENT film from an empty
    # premise — and charges you for it.
    if job_id and not premise.strip():
        from visci.config import OUTPUT_DIR

        state_file = OUTPUT_DIR / job_id / "state.json"
        if not state_file.exists():
            console.print(
                f"[bold red]No saved state for job {job_id}.[/]\n"
                f"Expected {state_file}\n\n"
                f"Resuming needs an existing job. To start a new film, "
                f"pass a premise instead."
            )
            raise typer.Exit(1)

        from visci.models import JobState

        saved = JobState.model_validate_json(state_file.read_text())
        if not saved.premise.strip():
            console.print(
                f"[bold red]Job {job_id} has no premise saved.[/]\n"
                f"This state is unusable — start a new job with a premise."
            )
            raise typer.Exit(1)

        # Show what is actually being resumed, so a wrong job id is obvious
        # BEFORE money is spent rather than after.
        premise = saved.premise
        mode = saved.mode
        fmt = saved.format

    # A film cannot be generated from nothing. Without this, an empty premise
    # produces an arbitrary story and bills you for it.
    if not premise.strip():
        console.print(
            "[bold red]No premise given.[/]\n"
            'Provide one:  visci generate "your movie idea"'
        )
        raise typer.Exit(1)

    # --real implies real story generation too.
    story_real = story_real or real

    # Cost scales with shot count: a 4-shot short is roughly a quarter of a
    # 15-shot film, plus the fixed script and reference-sheet cost.
    if real:
        per_shot = 0.53 if draft else 0.93   # video + keyframe
        fixed = 2.2                          # script + character sheets
        estimate = fixed + per_shot * fmt.shot_count
        tier = "Veo Lite (draft)" if draft else "Veo Fast"
        engine_label = f"[red]REAL — {tier}, ~${estimate:.2f}[/]"
    elif story_real:
        estimate = 0.60
        engine_label = "[yellow]REAL SCRIPT — mock video, ~$0.60[/]"
    else:
        estimate = 0.0
        engine_label = "[green]MOCK — free and instant[/]"

    console.print(
        Panel(
            f"[bold]{premise}[/]\n\n"
            f"mode:   {mode.value}\n"
            f"length: {fmt.label}\n"
            f"job:    {job_id}\n"
            f"engine: {engine_label}",
            title="visci",
            border_style="red" if real else "cyan",
        )
    )

    # Confirm before spending anything meaningful. A mistyped flag that quietly
    # spends $17 is a bad experience; one that asks first is not.
    if real and not yes:
        typer.confirm(
            f"This will spend roughly ${estimate:.2f} in API calls. Continue?",
            abort=True,
        )

    try:
        providers = build_providers(story_real, real, draft)
    except RuntimeError as exc:
        # Configuration problems (missing/invalid key) should read as a clear
        # setup message, not a stack trace.
        console.print(f"\n[bold red]Setup problem:[/] {exc}")
        raise typer.Exit(1) from exc

    job = Job(job_id, premise, mode, providers, fmt)

    try:
        final = job.run()
    except Exception as exc:  # top level: catch everything, report cleanly
        # Persist the failure, not just print it. Without this a killed CLI run
        # leaves state.json saying "Generating shots" forever, and the web UI
        # polls a job that will never progress.
        job.state.error = str(exc)
        job.save()
        console.print(f"\n[bold red]Failed:[/] {exc}")
        console.print(
            f"[dim]State saved. Resume with: visci generate '' --job-id {job_id}[/]"
        )
        raise typer.Exit(1) from exc

    console.print(f"\n[bold green]Done[/] → {final}")
    if job.ledger.entries:
        console.print("\n[bold]Spend[/]")
        console.print(job.ledger.summary())
    else:
        console.print("[dim]No spend — mock mode.[/]")


@app.command()
def watch(
    job_id: str = typer.Argument(
        None, help="Job to follow. Defaults to the most recent."
    ),
) -> None:
    """
    Follow a running job live.

    `show` is a snapshot; this refreshes. Useful when a film is being generated
    from the web UI and you want the per-shot detail that the browser does not
    surface — which stage each shot is at, what has been refused, and what the
    run has cost so far.

    Reads `state.json`, so it never interferes with the run itself.
    """
    import time

    from rich.live import Live
    from rich.table import Table

    from visci.config import OUTPUT_DIR
    from visci.models import JobState

    if job_id is None:
        # Most recently modified job directory — almost always the one running.
        dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir()] if OUTPUT_DIR.exists() else []
        if not dirs:
            console.print("[red]No jobs found.[/]")
            raise typer.Exit(1)
        job_id = max(dirs, key=lambda d: d.stat().st_mtime).name
        console.print(f"[dim]Following {job_id}[/]\n")

    state_file = OUTPUT_DIR / job_id / "state.json"
    if not state_file.exists():
        console.print(f"[red]No job at {state_file}[/]")
        raise typer.Exit(1)

    def render() -> Table:
        state = JobState.model_validate_json(state_file.read_text())

        table = Table(
            title=state.bible.title if state.bible else "Working…",
            caption=(
                f"{state.format.label} · ${state.cost_usd:.2f}"
                + (f" · {state.error[:60]}" if state.error else "")
            ),
            expand=True,
        )
        table.add_column("#", width=3, justify="right")
        table.add_column("status", width=9)
        table.add_column("framing", width=18)
        table.add_column("action", overflow="ellipsis", no_wrap=True)

        if not state.script:
            stage = (
                "writing the shot list"
                if state.bible
                else "building the bible"
                if state.treatment
                else "expanding the premise"
            )
            table.add_row("", "[yellow]…[/]", stage, "")
            return table

        for shot in state.script.shots:
            if shot.refused:
                status = "[red]refused[/]"
            elif shot.video_path and Path(shot.video_path).exists():
                status = "[green]ready[/]"
            elif shot.keyframe_path:
                status = "[yellow]filming[/]"
            else:
                status = "[dim]queued[/]"
            table.add_row(
                str(shot.index + 1),
                status,
                shot.framing.value,
                (shot.line or shot.action)[:80],
            )
        return table

    try:
        with Live(render(), refresh_per_second=1, console=console) as live:
            while True:
                time.sleep(2)
                live.update(render())
                state = JobState.model_validate_json(state_file.read_text())
                if state.final_video_path or state.error:
                    break
    except KeyboardInterrupt:
        pass

    console.print("\n[dim]Stopped following.[/]")


@app.command()
def check() -> None:
    """
    Verify API keys work, using the cheapest possible call.

    Costs a fraction of a cent. Worth running before a real generation so a bad
    key fails here rather than three stages into a job.
    """
    from visci.config import settings as s

    key = (s.anthropic_api_key or "").strip()
    if not key:
        console.print("[red]✗[/] ANTHROPIC_API_KEY not set in .env")
        raise typer.Exit(1)
    if not key.startswith("sk-ant-"):
        console.print(
            f"[red]✗[/] ANTHROPIC_API_KEY looks wrong "
            f"(starts with {key[:7]!r}, expected 'sk-ant-')"
        )
        raise typer.Exit(1)

    console.print("[dim]Testing Anthropic key…[/]")
    try:
        from anthropic import Anthropic

        client = Anthropic(api_key=key, timeout=30.0, max_retries=1)
        response = client.messages.create(
            model=s.claude_model,
            max_tokens=10,
            messages=[{"role": "user", "content": "Reply with the word: ok"}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        console.print(f"[green]✓[/] Anthropic key works — model replied {text.strip()!r}")
    except Exception as exc:
        console.print(f"[red]✗[/] Anthropic call failed: {exc}")
        raise typer.Exit(1) from exc

    gemini = (s.gemini_api_key or "").strip()
    if gemini:
        console.print("[dim]Gemini key present (not tested until media providers exist).[/]")
    else:
        console.print("[yellow]![/] GEMINI_API_KEY not set — needed for video, week 2.")


@app.command()
def assemble_partial(
    job_id: str = typer.Argument(..., help="Job id to assemble."),
) -> None:
    """
    Assemble whatever clips exist into a watchable film.

    Useful when a run is interrupted — by a quota ceiling, a refusal, or a
    crash. Ten of fifteen shots is still 80 seconds of real film, and being
    able to watch it is how you judge whether the pipeline is producing
    something worth finishing.

    Costs nothing: pure FFmpeg over clips already paid for.
    """
    from visci.config import OUTPUT_DIR
    from visci.models import JobState
    from visci.pipeline.assemble import assemble

    d = OUTPUT_DIR / job_id
    state_file = d / "state.json"
    if not state_file.exists():
        console.print(f"[red]No job found at {state_file}[/]")
        raise typer.Exit(1)

    state = JobState.model_validate_json(state_file.read_text())
    if not state.script:
        console.print("[red]This job has no script yet — nothing to assemble.[/]")
        raise typer.Exit(1)

    # Shots without a clip on disk are marked refused so assembly skips them,
    # which is the same path a genuinely refused shot takes.
    present, missing = [], []
    for shot in state.script.shots:
        clip = d / "clips" / f"shot_{shot.index:02d}.mp4"
        if clip.exists():
            shot.video_path = str(clip)
            shot.refused = False
            present.append(shot.index)
        else:
            shot.refused = True
            missing.append(shot.index)

    if not present:
        console.print("[red]No clips on disk yet.[/]")
        raise typer.Exit(1)

    out = d / f"{job_id}_partial.mp4"
    assemble(state.script, out, d / "work")

    seconds = len(present) * 8
    console.print(f"\n[bold green]Assembled[/] → {out}")
    console.print(f"  {len(present)}/{len(state.script.shots)} shots · {seconds}s")
    if missing:
        console.print(f"  [yellow]missing:[/] {missing}")
        console.print(
            f"  [dim]Resume to fill them: "
            f"visci generate '' --job-id {job_id} --real --draft[/]"
        )


@app.command()
def show(job_id: str = typer.Argument(..., help="Job id to inspect.")) -> None:
    """
    Print a job's script in readable form.

    Reading what Claude actually wrote is how you tune the prompts. Digging
    through state.json to do it would make that loop tedious enough that you
    would stop doing it.
    """
    from visci.config import OUTPUT_DIR
    from visci.models import JobState

    state_file = OUTPUT_DIR / job_id / "state.json"
    if not state_file.exists():
        console.print(f"[red]No job found at {state_file}[/]")
        raise typer.Exit(1)

    state = JobState.model_validate_json(state_file.read_text())

    if state.bible:
        b = state.bible
        console.print(Panel(f"[bold]{b.title}[/]\n{b.logline}", border_style="cyan"))
        console.print(f"[dim]Tone:[/] {b.tone}")
        console.print(f"[dim]Style:[/] {b.visual_style}")
        console.print(f"[dim]Palette:[/] {b.color_palette}")
        console.print(f"[dim]Lens:[/] {b.lens_language}\n")

        for c in b.characters:
            console.print(f"[bold yellow]{c.name}[/] — {c.role}")
            console.print(f"  [dim]Face:[/] {c.face}")
            console.print(f"  [dim]Build:[/] {c.build}")
            console.print(f"  [dim]Wears:[/] {c.wardrobe}")
            console.print(f"  [dim]Voice:[/] {c.voice_description}\n")

        for loc in b.locations:
            console.print(f"[bold green]{loc.name}[/]")
            console.print(f"  {loc.description}")
            console.print(f"  [dim]Light:[/] {loc.lighting}\n")

    if state.script:
        console.print("[bold]SHOT LIST[/]\n")
        for s in state.script.shots:
            who = ", ".join(s.character_names) or "—"
            console.print(
                f"[cyan]{s.index:>2}[/] [dim]{s.framing.value:<20}[/] {who}"
            )
            console.print(f"     {s.action}")
            if s.line:
                speaker = f"{s.speaker}: " if s.speaker else ""
                console.print(f"     [italic yellow]{speaker}“{s.line}”[/]")
            console.print()

    if state.cost_usd:
        console.print(f"[dim]Spent: ${state.cost_usd:.4f}[/]")


@app.command()
def config() -> None:
    """Show current settings and whether keys are present."""
    console.print(
        Panel(
            f"mock_mode:      {settings.mock_mode}\n"
            f"claude_model:   {settings.claude_model}\n"
            f"veo_model:      {settings.veo_model}\n"
            f"image_model:    {settings.image_model}\n"
            f"tts_model:      {settings.tts_model}\n"
            f"\n"
            f"anthropic key:  {'set' if settings.anthropic_api_key else 'not set'}\n"
            f"gemini key:     {'set' if settings.gemini_api_key else 'not set'}\n"
            f"\n"
            f"per-job cap:    ${settings.max_cost_per_job_usd:.2f}\n"
            f"per-day cap:    ${settings.max_cost_per_day_usd:.2f}",
            title="config",
            border_style="cyan",
        )
    )


if __name__ == "__main__":
    app()
