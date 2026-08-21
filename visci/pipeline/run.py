"""
The orchestrator. Runs every stage in order and saves state as it goes.

RESUMABILITY
------------
After each stage, the whole JobState is written to disk as JSON. That single
habit is what makes a failed run recoverable: if shot 12 of 15 fails, the
treatment, bible, script, and first 11 clips are all still on disk. Re-running
picks up where it stopped instead of starting over.

That matters because starting over costs $17 and 15 minutes. In week 6 this
logic moves into Prefect, which does the same thing with retries and
observability built in — but the shape stays identical, and understanding it
here makes that migration obvious rather than magical.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from visci.config import OUTPUT_DIR, settings
from visci.ledger import BudgetExceeded, Ledger
from visci.models import JobState, Mode, Script, Shot
from visci.pipeline.assemble import assemble, probe_duration
from visci.pipeline.concurrency import ConcurrencyController, jittered_delay
from visci.providers.base import ContentRefused, Providers

console = Console()


class Job:
    """One film-generation run, with everything it needs to resume."""

    def __init__(self, job_id: str, premise: str, mode: Mode, providers: Providers) -> None:
        self.dir = OUTPUT_DIR / job_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.providers = providers
        self.ledger = Ledger(job_id)
        self.refused_shots: list[int] = []

        # Resume if this job already exists, otherwise start fresh.
        if self.state_file.exists():
            self.state = JobState.model_validate_json(self.state_file.read_text())
            console.print(f"[yellow]Resuming job {job_id}[/]")
        else:
            self.state = JobState(job_id=job_id, premise=premise, mode=mode)

    def save(self) -> None:
        """Persist state. Called after every stage — cheap insurance."""
        self.state.cost_usd = self.ledger.job_total_usd
        self.state_file.write_text(self.state.model_dump_json(indent=2))

    # ------------------------------------------------------------- stages --

    def run(self) -> Path:
        """Execute the pipeline, skipping any stage already completed."""

        # -- 1. Treatment ---------------------------------------------------
        if self.state.treatment is None:
            console.print("[bold cyan]1/6[/] Expanding premise into a treatment")
            treatment, cost = self.providers.story.treatment(
                self.state.premise, self.state.mode
            )
            self.ledger.spend(cost)
            self.state.treatment = treatment
            self.save()
        console.print(f"     [green]✓[/] {self.state.treatment.title}")

        # -- 2. Story bible -------------------------------------------------
        if self.state.bible is None:
            console.print("[bold cyan]2/6[/] Building the story bible")
            bible, cost = self.providers.story.bible(self.state.treatment, self.state.mode)
            self.ledger.spend(cost)
            self.state.bible = bible
            self.save()
        console.print(
            f"     [green]✓[/] {len(self.state.bible.characters)} character(s), "
            f"{len(self.state.bible.locations)} location(s)"
        )

        # -- 3. Script / shot list ------------------------------------------
        if self.state.script is None:
            console.print("[bold cyan]3/6[/] Writing the shot list")
            script, cost = self.providers.story.script(
                self.state.bible, self.state.treatment
            )
            self.ledger.spend(cost)
            self.state.script = script
            self.save()
        script = self.state.script
        console.print(
            f"     [green]✓[/] {len(script.shots)} shots = "
            f"{script.runtime_seconds()}s runtime"
        )

        # -- 3b. Preflight --------------------------------------------------
        # Validate the video config BEFORE spending on reference sheets and
        # keyframes. A malformed config otherwise fails at the first video call,
        # after ~$2 of images have already been paid for.
        preflight = getattr(self.providers.video, "preflight", None)
        if preflight:
            preflight()

        # -- 4. Character reference sheets ----------------------------------
        # Generated once, then reused as reference input on every keyframe.
        # This is what keeps faces consistent across shots.
        refs_dir = self.dir / "references"
        for character in self.state.bible.characters:
            if character.reference_image_paths:
                continue
            console.print(f"[bold cyan]4/6[/] Reference sheet: {character.name}")
            paths, cost = self.providers.image.reference_sheet(
                character, self.state.bible, refs_dir
            )
            self.ledger.spend(cost)
            character.reference_image_paths = [str(p) for p in paths]
            self.save()
        console.print("     [green]✓[/] reference sheets ready")

        # -- 5. Keyframes and video, generated concurrently -----------------
        self._generate_shots(script)

        if self.refused_shots:
            self._report_refusals(script)

        # -- 5b. Narration audio (narration mode only) ----------------------
        # Dialogue mode skips this entirely: Veo already generated lip-synced
        # speech inside each clip.
        if self.state.mode is Mode.NARRATION:
            console.print("[bold cyan]5/6[/] Synthesising narration")
            voice_id = self.state.bible.narrator_voice_id or "Charon"
            for shot in script.shots:
                if not shot.line or shot.audio_path:
                    continue
                audio_path = self.dir / "audio" / f"shot_{shot.index:02d}.wav"
                audio, cost = self.providers.voice.speak(shot.line, voice_id, audio_path)
                self.ledger.spend(cost)
                shot.audio_path = str(audio)

                # MEASURE the real duration rather than trusting a word-count
                # estimate. A line that overruns its 8-second shot desynchronises
                # the film, and small drift compounds across 15 shots.
                shot.audio_duration_seconds = probe_duration(audio)
                self.save()

            self._warn_on_overruns(script)

        # -- 6. Assembly ----------------------------------------------------
        console.print("[bold cyan]6/6[/] Assembling final film")
        final = self.dir / f"{self.state.job_id}.mp4"
        assemble(script, final, self.dir / "work")
        self.state.final_video_path = str(final)
        self.save()

        return final

    def _generate_shots(self, script: Script) -> None:
        """
        Generate every outstanding shot, several at a time.

        WHY CONCURRENT
        --------------
        Each shot is 1-3 minutes of waiting on Google. Sequentially that is 30+
        minutes for a film; at 3-5 at once it is under 10. The work is entirely
        I/O-bound, so threads are the right tool — no GIL contention, no need
        for async.

        WHY ADAPTIVE
        ------------
        A fixed concurrency guesses at the provider's ceiling. Too low wastes
        time; too high trips rate limits, and 5 workers hitting one together
        back off together, producing bursts of failure rather than throughput.
        The controller starts low and finds the real ceiling.

        WHY STATE IS SAVED UNDER A LOCK
        -------------------------------
        Workers mutate their own Shot objects, but `save()` serialises the whole
        JobState. Two threads writing state.json at once would corrupt it — and
        that file is the only thing making a run resumable.
        """
        pending = [
            s
            for s in script.shots
            if not (s.video_path and Path(s.video_path).exists())
        ]
        done_already = len(script.shots) - len(pending)

        if not pending:
            console.print("     [green]✓[/] all shots already generated")
            return

        controller = ConcurrencyController(
            start=min(settings.max_concurrent_videos, 3),
            maximum=settings.max_concurrent_videos,
        )
        save_lock = threading.Lock()

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold cyan]5/6[/] {task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task(
                f"Generating shots ({controller.limit} at a time)",
                total=len(script.shots),
                completed=done_already,
            )

            def worker(shot: Shot) -> None:
                # acquire() blocks until the controller has room, re-reading the
                # limit each time it wakes — so a mid-run change to concurrency
                # takes effect immediately. A fixed-capacity Semaphore could not
                # do this; its size is set once at construction.
                if not controller.acquire():
                    return  # run was aborted while we waited
                try:
                    self._generate_one_shot(
                        shot, controller, progress, task, save_lock
                    )
                finally:
                    controller.release()

            with ThreadPoolExecutor(max_workers=settings.max_concurrent_videos) as pool:
                futures = [pool.submit(worker, shot) for shot in pending]
                for future in as_completed(futures):
                    # Surface any exception a worker did not handle itself.
                    future.result()

        if controller.aborted:
            raise RuntimeError(controller.abort_reason or "run aborted")

    def _generate_one_shot(
        self,
        shot: Shot,
        controller: ConcurrencyController,
        progress,
        task,
        save_lock: threading.Lock,
    ) -> None:
        """Keyframe then video for one shot, with rate-limit and refusal handling."""
        from visci.providers.base import ContentRefused

        attempt = 0
        while True:
            attempt += 1
            if controller.should_stop():
                return

            # The ledger gets a veto BEFORE each shot dispatches. Concurrency
            # means a runaway bug spends 5x faster, so the spend ceiling must be
            # able to stop work rather than only report on it afterwards.
            try:
                self._check_budget()
            except BudgetExceeded as exc:
                controller.abort(str(exc))
                return

            try:
                if not shot.keyframe_path:
                    kf_path = self.dir / "keyframes" / f"shot_{shot.index:02d}.png"
                    kf, cost = self.providers.image.keyframe(
                        shot, self.state.bible, kf_path
                    )
                    with save_lock:
                        self.ledger.spend(cost)
                    shot.keyframe_path = str(kf)

                vid_path = self.dir / "clips" / f"shot_{shot.index:02d}.mp4"
                vid, cost = self.providers.video.generate(
                    shot, self.state.bible, Path(shot.keyframe_path), vid_path
                )
                with save_lock:
                    self.ledger.spend(cost)
                shot.video_path = str(vid)

                controller.on_success()
                self._finish_shot(shot, progress, task, save_lock)
                return

            except ContentRefused as refusal:
                # Pull the camera back and try once more. Observed refusals
                # cluster on tight close-ups of a character in distress; the
                # same beat at a wider framing renders.
                if self._retry_wider(shot, progress):
                    controller.on_success()
                    self._finish_shot(shot, progress, task, save_lock)
                    return

                # A refused shot must NOT kill the film. Fourteen good shots
                # with one gap is recoverable by rewording one line; a dead run
                # after paying for eleven clips is not.
                shot.refused = True
                shot.refusal_reason = str(refusal)
                with save_lock:
                    self.refused_shots.append(shot.index)
                progress.console.print(
                    f"  [yellow]shot {shot.index:02d} refused — continuing[/]"
                )
                self._finish_shot(shot, progress, task, save_lock)
                return

            except Exception as exc:  # noqa: BLE001 — a worker must never
                # propagate an unexpected error: one bad shot killing the whole
                # film is exactly the failure this design exists to prevent.
                # _handle_worker_error decides retry vs give-up per error type.
                if not self._handle_worker_error(
                    exc, shot, attempt, controller, progress
                ):
                    self._finish_shot(shot, progress, task, save_lock)
                    return

    def _handle_worker_error(
        self, exc: Exception, shot: Shot, attempt: int, controller, progress
    ) -> bool:
        """
        Decide what to do about a worker's exception.

        Returns True to retry the shot, False to give up on it.
        """
        from visci.providers.gemini import QuotaExhausted

        message = str(exc)

        # A quota ceiling does not clear by waiting, so it stops the WHOLE run.
        # Without this, each of N workers would independently burn its own
        # backoff discovering the same wall.
        if isinstance(exc, QuotaExhausted) or "exceeded your current quota" in message:
            controller.abort(message)
            progress.console.print("\n[bold red]Quota reached[/] — stopping run.")
            return False

        # A rate limit means "too fast", not "too much". Narrow the pipe and try
        # again after a jittered pause so workers do not retry in lockstep.
        if "429" in message or "rate limit" in message.lower():
            if attempt > settings.max_shot_retries + 2:
                progress.console.print(
                    f"  [red]shot {shot.index:02d} gave up after {attempt} "
                    f"rate-limited attempts[/]"
                )
                return False
            new_limit = controller.on_rate_limit()
            delay = jittered_delay(30 * attempt)
            progress.console.print(
                f"  [yellow]rate limited — concurrency now {new_limit}, "
                f"retrying shot {shot.index:02d} in {delay:.0f}s[/]"
            )
            time.sleep(delay)
            return True

        # Anything else is a real error for this shot. Report it and move on
        # rather than killing the film.
        progress.console.print(
            f"  [red]shot {shot.index:02d} failed: {type(exc).__name__}: "
            f"{message[:120]}[/]"
        )
        return False

    def _finish_shot(self, shot: Shot, progress, task, save_lock) -> None:
        """Persist state and advance the bar, serialised across threads."""
        with save_lock:
            self.save()
            progress.advance(task)

    def _check_budget(self) -> None:
        """Raise if this job has already reached its spend ceiling."""
        if self.ledger.job_total_usd >= settings.max_cost_per_job_usd:
            raise BudgetExceeded(
                f"Job {self.state.job_id} reached its "
                f"${settings.max_cost_per_job_usd:.2f} ceiling."
            )

    def _retry_wider(self, shot, progress) -> bool:
        """
        Retry a refused shot at the next-wider framing.

        Regenerates BOTH the keyframe and the clip, because the framing lives in
        the still: animating a close-up keyframe with a "medium shot" prompt
        widens nothing.

        Costs one extra image (~$0.13) plus one clip, and only fires on shots
        that would otherwise be lost entirely. Returns True if the wider version
        rendered.
        """
        from visci.prompts.visual import WIDER_FRAMING

        wider = WIDER_FRAMING.get(shot.framing)
        if wider is None:
            # Already a wide framing — the refusal is about content, not scale,
            # so pulling back further would not help.
            return False

        progress.console.print(
            f"  [yellow]shot {shot.index:02d} refused at "
            f"{shot.framing.value} — retrying at {wider.value}[/]"
        )

        original = shot.framing
        shot.framing = wider
        try:
            kf_path = self.dir / "keyframes" / f"shot_{shot.index:02d}_wide.png"
            kf, cost = self.providers.image.keyframe(shot, self.state.bible, kf_path)
            self.ledger.spend(cost)

            vid_path = self.dir / "clips" / f"shot_{shot.index:02d}.mp4"
            vid, cost = self.providers.video.generate(
                shot, self.state.bible, kf, vid_path
            )
            self.ledger.spend(cost)

            shot.keyframe_path = str(kf)
            shot.video_path = str(vid)
            progress.console.print(
                f"  [green]shot {shot.index:02d} rendered at {wider.value}[/]"
            )
            return True

        except ContentRefused:
            # Restore the original framing so the saved script reflects what was
            # actually written, not our failed workaround.
            shot.framing = original
            return False

    def _report_refusals(self, script: Script) -> None:
        """
        Tell the user exactly which shots were refused and what to do.

        A refusal is a content problem the user can fix by rewording one line —
        so the report must name the shot and show its text, not just say
        "something was blocked".
        """
        console.print(
            f"\n[yellow]⚠ {len(self.refused_shots)} shot(s) refused by the "
            f"video model's safety filters.[/]"
        )
        console.print(
            "[dim]The film was assembled without them and is "
            f"{(len(script.shots) - len(self.refused_shots)) * 8}s instead of "
            f"{len(script.shots) * 8}s.[/]\n"
        )

        for index in self.refused_shots:
            shot = script.shots[index]
            console.print(f"  [bold]Shot {index}[/] — {shot.framing.value}")
            console.print(f"    {shot.action[:150]}")
            if shot.line:
                console.print(f'    [italic]"{shot.line}"[/]')
            console.print()

        console.print(
            "[dim]To fill the gaps: edit the `action` or `line` of those shots "
            "in state.json,\nthen re-run with --job-id to regenerate only "
            "those shots.[/]"
        )

    def _warn_on_overruns(self, script: Script) -> None:
        """Report any narration line that does not fit its shot."""
        from visci.models import (
            NARRATION_OVERRUN_TOLERANCE_SECONDS,
            SHOT_DURATION_SECONDS,
        )

        limit = SHOT_DURATION_SECONDS + NARRATION_OVERRUN_TOLERANCE_SECONDS
        overruns = [
            (s.index, s.audio_duration_seconds)
            for s in script.shots
            if s.audio_duration_seconds and s.audio_duration_seconds > limit
        ]
        if overruns:
            console.print(
                f"[yellow]⚠ {len(overruns)} narration line(s) exceed "
                f"{SHOT_DURATION_SECONDS}s and will be trimmed:[/]"
            )
            for index, duration in overruns:
                console.print(f"     shot {index}: {duration:.2f}s")
