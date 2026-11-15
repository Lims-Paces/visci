"""
Tests for the pipeline.

These run in about a second and cost nothing, which is the whole point of the
mock provider layer. Without it, testing assembly logic would mean spending $17
and waiting 15 minutes per run — so in practice you would not test it at all.
"""

import pytest

from visci.config import settings
from visci.ledger import BudgetExceeded, Ledger
from visci.models import (
    SHOT_COUNT,
    SHOT_DURATION_SECONDS,
    TARGET_RUNTIME_SECONDS,
    Format,
    Framing,
    Mode,
    Shot,
)
from visci.pipeline.assemble import probe_duration
from visci.pipeline.run import Job
from visci.providers.base import CostReport, Providers
from visci.providers.mock import (
    MockImageProvider,
    MockStoryProvider,
    MockVideoProvider,
    MockVoiceProvider,
)


@pytest.fixture(autouse=True)
def isolate_spend(tmp_path, monkeypatch):
    """
    Point the daily spend ledger at a temp directory for every test.

    Without this, tests write to the real `output/.daily_spend.json` — so a test
    that records spend both corrupts your actual daily total AND fails once the
    real total approaches the cap. Both happened.
    """
    monkeypatch.setattr("visci.ledger.OUTPUT_DIR", tmp_path)


@pytest.fixture
def providers() -> Providers:
    return Providers(
        story=MockStoryProvider(),
        image=MockImageProvider(),
        video=MockVideoProvider(),
        voice=MockVoiceProvider(),
    )


# --------------------------------------------------------------- constants --


def test_shot_arithmetic_is_consistent():
    """The locked constraints must actually divide evenly."""
    assert SHOT_COUNT * SHOT_DURATION_SECONDS == TARGET_RUNTIME_SECONDS
    assert SHOT_COUNT == 15


def test_formats_divide_evenly_into_shots():
    """
    Runtime can only move in 8-second steps, because Veo requires 8s clips when
    using reference images. Every format must respect that.
    """
    for fmt in Format:
        assert fmt.runtime_seconds == fmt.shot_count * SHOT_DURATION_SECONDS
        assert fmt.shot_count >= 1

    # SHORT is really 32s; "30 seconds" is a deliberate rounding for the UI.
    assert Format.SHORT.shot_count == 4
    assert Format.SHORT.runtime_seconds == 32
    assert Format.SHORT.label == "30 seconds"
    assert Format.FILM.shot_count == 15


def test_short_is_the_default():
    """
    A 30-second short costs roughly a third of a full film, which makes it the
    right default and a far better free tier.
    """
    from visci.models import DEFAULT_FORMAT, JobState

    assert DEFAULT_FORMAT is Format.SHORT
    # A job created without an explicit format gets the short.
    assert JobState(job_id="x", premise="y", mode=Mode.DIALOGUE).format is Format.SHORT


@pytest.mark.parametrize("fmt", list(Format))
def test_each_format_produces_a_film_of_its_own_length(
    tmp_path, monkeypatch, providers, fmt
):
    """
    Both formats must produce a correctly-sized film end to end.

    This is the test that would catch a format whose shot count and runtime
    disagree — the film would come out the wrong length with no other symptom.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job(f"fmt-{fmt.value}", "a test premise", Mode.DIALOGUE, providers, fmt)
    final = job.run()

    assert final.exists()
    assert len(job.state.script.shots) == fmt.shot_count
    assert probe_duration(final) == pytest.approx(fmt.runtime_seconds, abs=1.0)


def test_short_prompt_asks_for_a_single_moment_not_three_acts():
    """
    Four shots is a different FORM, not a shortened film.

    Asking for three acts in four shots produces a rushed setup, no middle, and
    a punchline. The prompt must branch, not just change a number.
    """
    from visci.prompts.story import treatment_prompt

    short = treatment_prompt("a premise", Mode.DIALOGUE, Format.SHORT).lower()
    film = treatment_prompt("a premise", Mode.DIALOGUE, Format.FILM).lower()

    assert "three-act" not in short, "a short must not be asked for three acts"
    assert "single charged moment" in short or "no room for three acts" in short
    assert "exactly 4 shots" in short

    assert "three-act" in film
    assert "exactly 15 shots" in film


def test_shot_rejects_more_than_two_characters():
    """
    A 9:16 frame cannot hold a crowd. The schema enforces this so Claude
    physically cannot request a shot we know will generate badly.
    """
    with pytest.raises(ValueError):
        Shot(
            index=0,
            character_names=["A", "B", "C"],
            location_name="anywhere",
            framing=Framing.MEDIUM,
            action="three people talk",
        )


# ------------------------------------------------------------------ ledger --


def test_ledger_blocks_spend_over_job_ceiling():
    """
    The spend ceiling must HALT, not warn. This is the guard between a retry
    bug and a drained card.
    """
    ledger = Ledger("test-job")
    with pytest.raises(BudgetExceeded):
        ledger.spend(CostReport(settings.max_cost_per_job_usd + 1.0, "oversized call"))


def test_ledger_ignores_free_calls():
    """Mock-mode calls cost nothing and must not touch the daily file."""
    ledger = Ledger("test-job")
    ledger.spend(CostReport(0.0, "mock"))
    assert ledger.job_total_usd == 0.0


def test_ledger_accumulates():
    ledger = Ledger("test-job")
    ledger.spend(CostReport(1.0, "a"))
    ledger.spend(CostReport(2.5, "b"))
    assert ledger.job_total_usd == pytest.approx(3.5)


def test_spend_that_crosses_the_ceiling_is_still_recorded(monkeypatch):
    """
    spend() runs AFTER a paid call, so the money is already gone. It must be
    recorded even when it crosses a ceiling — otherwise the job total stays
    under the cap and the run keeps dispatching paid calls it never counts.
    """
    monkeypatch.setattr(settings, "max_cost_per_job_usd", 3.5)
    ledger = Ledger("test-job")
    for _ in range(3):
        ledger.spend(CostReport(1.0, "clip"))
    with pytest.raises(BudgetExceeded):
        ledger.spend(CostReport(1.0, "clip"))
    assert ledger.job_total_usd == pytest.approx(4.0), "the crossing call must be recorded"


def test_reservation_blocks_a_call_that_would_not_fit(monkeypatch):
    """The ceiling vetoes a call BEFORE it is made, counting calls in flight."""
    monkeypatch.setattr(settings, "max_cost_per_job_usd", 3.5)
    ledger = Ledger("test-job")
    ledger.spend(CostReport(2.0, "done already"))
    ledger.reserve(1.0)  # one call in flight: 3.0 committed
    with pytest.raises(BudgetExceeded):
        ledger.reserve(1.0)  # a second concurrent call would reach 4.0
    ledger.release(1.0)
    assert ledger.job_total_usd == pytest.approx(2.0), "reserving spends nothing"


class _PaidVideo(MockVideoProvider):
    """A mock that charges $1 per clip, so ceilings can be tested for free."""

    def __init__(self, estimated: float | None) -> None:
        super().__init__()
        self.calls = 0
        if estimated is not None:
            self.estimated_clip_usd = estimated

    def generate(self, shot, bible, keyframe, out_path):
        path, _ = super().generate(shot, bible, keyframe, out_path)
        self.calls += 1  # a real provider would have charged by now
        return path, CostReport(1.0, f"video:shot:{shot.index:02d}")


@pytest.mark.parametrize("estimated", [1.0, None], ids=["with-estimate", "no-estimate"])
def test_spend_ceiling_halts_a_concurrent_run(tmp_path, monkeypatch, estimated):
    """
    Regression: a $3.50 ceiling once let $12 of clips through, recording $3.

    Every dollar spent must be recorded, and the run must stop. With a known
    per-call estimate, reservation stops it before crossing the ceiling at all.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr(settings, "max_cost_per_job_usd", 3.5)
    video = _PaidVideo(estimated)
    providers = Providers(
        story=MockStoryProvider(),
        image=MockImageProvider(),
        video=video,
        voice=MockVoiceProvider(),
    )

    job = Job("ceiling-test", "a test premise", Mode.DIALOGUE, providers, Format.FILM)
    with pytest.raises(RuntimeError, match="ceiling"):
        job.run()

    assert job.ledger.job_total_usd == pytest.approx(video.calls * 1.0), (
        f"spent ${video.calls}, but the ledger recorded ${job.ledger.job_total_usd}"
    )
    if estimated is not None:
        assert video.calls <= 3, "reservation must stop the run before the ceiling"
    else:
        # No estimate means the breach is only seen after a call returns; the
        # overshoot is then bounded by the calls already in flight.
        assert video.calls <= 3 + settings.max_concurrent_videos


def test_resumed_job_keeps_its_spend_history(tmp_path, monkeypatch, providers):
    """
    A resume must not reset the ledger to zero. If it did, every resume would
    get a fresh per-job budget, and save() would erase what was already spent.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)
    job = Job("resume-spend", "a test premise", Mode.DIALOGUE, providers)
    job.ledger.spend(CostReport(5.0, "earlier work"))
    job.save()

    resumed = Job("resume-spend", "a test premise", Mode.DIALOGUE, providers)
    assert resumed.ledger.job_total_usd == pytest.approx(5.0)
    resumed.save()
    assert resumed.state.cost_usd == pytest.approx(5.0)


def test_api_rejects_job_ids_that_could_escape_the_output_dir():
    """A crafted id must never be joined onto a filesystem path."""
    from fastapi import HTTPException

    from visci.api import get_clip, get_job, get_keyframe, resume_job

    for bad in ("..", "../etc", "20260101-000000-zzzzzz", "x" * 30):
        with pytest.raises(HTTPException) as err:
            get_job(bad)
        assert err.value.status_code == 404
        for route in (get_clip, get_keyframe):
            with pytest.raises(HTTPException):
                route(bad, 1)
        with pytest.raises(HTTPException):
            resume_job(bad)


def _saved_job(tmp_path, monkeypatch, providers, real: bool) -> str:
    """Write an unfinished job to disk, as the API would after an interruption."""
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)
    monkeypatch.setattr("visci.api.OUTPUT_DIR", tmp_path)
    job_id = "20260101-000000-abcdef"
    job = Job(job_id, "a test premise", Mode.DIALOGUE, providers)
    job.state.real = real
    job.state.draft = True
    job.save()
    return job_id


def test_resume_of_a_free_preview_stays_free(tmp_path, monkeypatch, providers):
    """A free preview must never be resumed as paid generation."""
    from fastapi import HTTPException

    from visci import api

    job_id = _saved_job(tmp_path, monkeypatch, providers, real=False)

    with pytest.raises(HTTPException) as err:
        api.resume_job(job_id, real=True)
    assert err.value.status_code == 400

    status = api.resume_job(job_id)  # no flags: continues as a free preview
    assert status.real is False
    thread = api._running.get(job_id)
    if thread:
        thread.join(timeout=60)
    assert api.get_job(job_id).finished


def test_resume_of_a_real_job_uses_real_providers(tmp_path, monkeypatch, providers):
    """
    A real film must resume with real providers, never mocks. With no API keys
    configured, choosing real providers fails setup (400) — proving the resume
    did not quietly fall back to splicing placeholder clips into a paid film.
    """
    from fastapi import HTTPException

    from visci import api

    job_id = _saved_job(tmp_path, monkeypatch, providers, real=True)
    monkeypatch.setattr(settings, "anthropic_api_key", "")
    monkeypatch.setattr(settings, "gemini_api_key", "")

    with pytest.raises(HTTPException) as err:
        api.resume_job(job_id)
    assert err.value.status_code == 400
    assert api.get_job(job_id).real is True


# ---------------------------------------------------------------- pipeline --


@pytest.mark.parametrize("mode", [Mode.DIALOGUE, Mode.NARRATION])
def test_full_pipeline_produces_a_correct_film(tmp_path, monkeypatch, providers, mode):
    """
    End-to-end: premise in, correctly-shaped film out.

    Checks the things that would silently break a real run — dimensions,
    duration, shot count.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job(f"test-{mode.value}", "a test premise", mode, providers, Format.FILM)
    final = job.run()

    assert final.exists()
    assert len(job.state.script.shots) == SHOT_COUNT
    assert job.state.script.runtime_seconds() == TARGET_RUNTIME_SECONDS

    # The film must be the real target length, measured not assumed.
    duration = probe_duration(final)
    assert duration == pytest.approx(TARGET_RUNTIME_SECONDS, abs=1.0)


def test_pipeline_resumes_without_redoing_work(tmp_path, monkeypatch, providers):
    """
    A half-finished job must resume, not restart.

    This is the behaviour that turns a $17 failure into a $5 one.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job("resume-test", "a test premise", Mode.DIALOGUE, providers)
    job.run()

    # Simulate a crash partway: destroy the last five clips.
    for shot in job.state.script.shots[10:]:
        from pathlib import Path

        Path(shot.video_path).unlink()

    # A fresh Job object with the same id reloads state from disk.
    resumed = Job("resume-test", "a test premise", Mode.DIALOGUE, providers)
    assert resumed.state.treatment is not None, "treatment should be reloaded, not regenerated"
    assert resumed.state.bible is not None, "bible should be reloaded, not regenerated"

    final = resumed.run()
    assert final.exists()


# ------------------------------------------------------------ real providers --
# These check configuration and prompt construction WITHOUT making API calls.
# Anything that would spend money stays out of the test suite.


def test_gemini_provider_rejects_missing_key(monkeypatch):
    """A missing key must fail immediately, not hang on network retries."""
    from visci.providers import gemini

    monkeypatch.setattr(gemini.settings, "gemini_api_key", "")
    with pytest.raises(RuntimeError, match="GEMINI_API_KEY is empty"):
        gemini._client()


def test_claude_provider_rejects_malformed_key(monkeypatch):
    """
    Key SHAPE is validated before any call.

    Without this, a placeholder value produces a long retry-and-backoff hang
    instead of an obvious error.
    """
    from visci.providers import claude

    monkeypatch.setattr(claude.settings, "anthropic_api_key", "not-a-real-key")
    with pytest.raises(RuntimeError, match="does not look like a real key"):
        claude.ClaudeStoryProvider()


def test_visual_prompts_quote_the_bible_verbatim(providers):
    """
    Consistency depends on character descriptions appearing UNCHANGED in every
    prompt. If a description is paraphrased between shots, the face drifts.
    """
    from visci.prompts.visual import keyframe_prompt, video_prompt

    treatment, _ = providers.story.treatment("x", Mode.DIALOGUE, Format.FILM)
    bible, _ = providers.story.bible(treatment, Mode.DIALOGUE, Format.FILM)
    script, _ = providers.story.script(bible, treatment, Format.FILM)

    character = bible.characters[0]
    shot = next(s for s in script.shots if character.name in s.character_names)

    prompt = keyframe_prompt(shot, bible)
    assert character.face in prompt, "face description must appear verbatim"
    assert character.wardrobe in prompt, "wardrobe must appear verbatim"
    assert bible.visual_style in prompt, "visual style must appear verbatim"

    # The VIDEO prompt deliberately does NOT repeat the style block.
    #
    # The keyframe is passed to Veo as the input image, so the still already
    # carries the grade, grain, and lighting — the clip cannot drift from a look
    # it is literally starting from. Re-describing it in words bloated the
    # prompt to ~500 words, of which only ~60 described the movement, diluting
    # attention on the one thing the video model actually has to invent.
    video = video_prompt(shot, bible)
    assert bible.visual_style not in video, (
        "video prompt should not repeat the style block — the keyframe carries it"
    )
    assert len(video.split()) < 350, "video prompt must stay focused on motion"

    # What it MUST carry is the action and explicit motion direction.
    assert shot.action in video
    assert "MOTION:" in video


def test_video_prompt_gives_motion_direction():
    """
    Generated movement reads as unnatural in a diagnosable way: bodies move all
    at once, at constant speed, without weight. Naming anticipation, sequencing,
    and settling explicitly is what fixes it — "natural motion" alone does not,
    because it does not say what natural motion consists of.
    """
    from visci.prompts.visual import MOTION_DIRECTION

    lowered = MOTION_DIRECTION.lower()
    assert "weight before" in lowered, "anticipation"
    assert "lead" in lowered and "follow" in lowered, "sequencing"
    assert "ease into stillness" in lowered, "settling"


def test_claude_camera_direction_is_sent_through_verbatim():
    """
    Claude writes the camera direction and we send it unchanged.

    An earlier version reduced this to a vague hint, on the theory that the
    video model "has seen more cinema" than our enum. That was backwards: Veo
    sees ONE keyframe and one paragraph, while Claude knows the whole film and
    the 9:16 frame. The model with more context should compose.
    """
    from visci.prompts.visual import camera_direction, keyframe_prompt

    shot = Shot(
        index=0,
        character_names=["A"],
        location_name="x",
        framing=Framing.CLOSE_UP,
        camera=(
            "85mm, her face in the upper third of the tall frame, the empty "
            "hallway running away below her"
        ),
        action="She looks out of the window at the rain.",
    )

    assert camera_direction(shot) == shot.camera, "must pass through unchanged"

    from visci.models import Mode, NarrationPerson, StoryBible

    bible = StoryBible(
        title="t", logline="l", mode=Mode.DIALOGUE,
        narration_person=NarrationPerson.THIRD,
        characters=[], locations=[],
        visual_style="s", color_palette="p", lens_language="ll", tone="tn",
    )
    assert shot.camera in keyframe_prompt(shot, bible)


def test_sensitive_shots_override_claude_camera_direction():
    """
    Safety beats craft on sensitive content.

    A tight frame on an injury is what gets refused, so the prescribed wider
    framing must replace whatever Claude wrote — a creative choice cannot be
    allowed to reintroduce the exact composition that fails.
    """
    from visci.models import is_sensitive
    from visci.prompts.visual import camera_direction

    shot = Shot(
        index=0,
        character_names=["A"],
        location_name="x",
        framing=Framing.CLOSE_UP,
        camera="extreme close-up, the bruise filling the entire frame",
        action="She covers the bruise on her cheek with concealer.",
    )

    assert is_sensitive(shot)
    result = camera_direction(shot)
    assert result != shot.camera, "sensitive shots must not use Claude's framing"
    assert "the room visible around them" in result


def test_story_prompt_asks_for_real_camera_direction():
    """
    The prompt must ask Claude to think about the vertical frame, not to echo
    a category back. Two people in 9:16 is the hard case and needs naming.
    """
    from visci.models import Mode, NarrationPerson, StoryBible, Treatment
    from visci.prompts.story import script_prompt

    bible = StoryBible(
        title="t", logline="l", mode=Mode.DIALOGUE,
        narration_person=NarrationPerson.THIRD,
        characters=[], locations=[],
        visual_style="s", color_palette="p", lens_language="ll", tone="tn",
    )
    prompt = script_prompt(bible, Treatment(title="t", logline="l", beats=[]),
                           Format.FILM).lower()

    assert "you are the cinematographer" in prompt
    assert "tall vertical frame" in prompt
    assert "two people" in prompt, "the hard case in 9:16 must be addressed"


def test_every_framing_has_prompt_language():
    """
    A framing added to the enum without prompt language would raise KeyError
    mid-generation — after money has already been spent on earlier shots.
    """
    from visci.prompts.visual import FRAMING_STRICT

    # Only the strict map needs full coverage now — ordinary shots use Claude's
    # own direction, so there is no lookup that can be missing an entry. But a
    # framing without strict phrasing would KeyError mid-generation on a
    # sensitive shot, after money has been spent on earlier ones.
    for framing in Framing:
        assert framing in FRAMING_STRICT, f"{framing} has no strict phrasing"


# ----------------------------------------------------------------- export --


def _sample_state(providers):
    from visci.models import JobState

    treatment, _ = providers.story.treatment("x", Mode.DIALOGUE, Format.FILM)
    bible, _ = providers.story.bible(treatment, Mode.DIALOGUE, Format.FILM)
    script, _ = providers.story.script(bible, treatment, Format.FILM)
    return JobState(
        job_id="export-test",
        premise="x",
        mode=Mode.DIALOGUE,
        treatment=treatment,
        bible=bible,
        script=script,
    )


def test_docx_uses_screenplay_typography(providers):
    """
    The exported script must look like a script.

    Courier 12pt at a 1.5" left margin with character names at 3.7" and
    dialogue at 2.5" from the page edge is the industry standard — it is what
    makes one page approximate one minute of screen time, and what makes a
    reader recognise the document as a screenplay rather than a memo.
    """
    from io import BytesIO

    from docx import Document
    from docx.shared import Inches

    from visci.export import script_docx

    doc = Document(BytesIO(script_docx(_sample_state(providers))))

    section = doc.sections[0]
    assert section.left_margin.inches == 1.5, "screenplay left margin"

    fonts = {r.font.name for p in doc.paragraphs for r in p.runs if r.font.name}
    assert fonts == {"Courier"}, f"expected Courier throughout, got {fonts}"

    # Find a character cue and check it and its dialogue are indented properly.
    found = False
    for i, p in enumerate(doc.paragraphs[:-1]):
        if p.text.isupper() and p.text.strip() and i + 1 < len(doc.paragraphs):
            indent = (p.paragraph_format.left_indent or Inches(0)).inches
            if indent == 2.2:
                dialogue = doc.paragraphs[i + 1]
                assert (
                    dialogue.paragraph_format.left_indent or Inches(0)
                ).inches == 1.0
                found = True
                break
    assert found, "no correctly indented character/dialogue pair found"


def test_fountain_is_importable_format(providers):
    """
    The Fountain file must carry the markers real screenwriting software
    parses, or it imports as a wall of plain text.
    """
    from visci.export import script_fountain

    text = script_fountain(_sample_state(providers))

    assert text.startswith("Title:"), "title page metadata comes first"
    assert "\nAuthor:" in text
    # A leading dot forces a scene heading regardless of the location's name.
    assert "\n." in text, "scene headings must be forced with a leading dot"
    # Dialogue is introduced by an all-caps character line.
    assert any(
        line.isupper() and line.strip() for line in text.splitlines()
    ), "character cues must be upper case"


# ------------------------------------------------------------ concurrency --


def test_controller_halves_on_rate_limit_and_climbs_on_success():
    """
    Rate limits must HALVE concurrency, not decrement it.

    Stepping down by one keeps re-tripping the limit; halving backs off fast
    enough to actually clear it. Climbing back must be gradual for the same
    reason in reverse.
    """
    from visci.pipeline.concurrency import ConcurrencyController

    c = ConcurrencyController(start=8, minimum=1, maximum=8, successes_before_increase=2)

    assert c.on_rate_limit() == 4
    assert c.on_rate_limit() == 2
    assert c.on_rate_limit() == 1
    assert c.on_rate_limit() == 1, "must not drop below the minimum"

    # Climbing needs sustained success, not a single one.
    c.on_success()
    assert c.limit == 1, "one success must not immediately widen the pipe"
    c.on_success()
    assert c.limit == 2


def test_controller_abort_is_visible_to_every_worker():
    """
    A quota ceiling must stop the WHOLE run.

    Without a shared abort flag, each of N workers independently discovers the
    same wall and burns its own backoff on a guaranteed failure.
    """
    from visci.pipeline.concurrency import ConcurrencyController

    c = ConcurrencyController()
    assert not c.should_stop()

    c.abort("quota reached")
    assert c.should_stop()
    assert c.abort_reason == "quota reached"

    # First reason wins — the original cause is the useful one.
    c.abort("something else")
    assert c.abort_reason == "quota reached"


def test_limit_changes_take_effect_at_runtime():
    """
    The gate must honour a limit that changes mid-run.

    A fixed-capacity Semaphore cannot do this — its size is set at construction,
    so halving the limit after a rate limit would have no actual effect and the
    pipeline would keep hammering the provider at the old rate.
    """
    from visci.pipeline.concurrency import ConcurrencyController

    c = ConcurrencyController(start=2, minimum=1, maximum=4)

    assert c.acquire()
    assert c.acquire()
    assert c._in_flight == 2

    # At the limit, so a third must wait. Drop the limit and release: even after
    # a release we are still at capacity for the NEW limit.
    c.on_rate_limit()  # 2 -> 1
    c.release()
    assert c._in_flight == 1
    assert c.limit == 1


def test_concurrency_recovers_after_rate_limits():
    """
    A transient rate limit must not permanently cripple throughput.

    Regression test: an early rate limit halved the limit to 1, and recovery
    was slow enough that the rest of the film ran effectively sequentially.
    """
    from visci.pipeline.concurrency import ConcurrencyController

    c = ConcurrencyController(start=4, minimum=1, maximum=8)
    c.on_rate_limit()
    c.on_rate_limit()
    assert c.limit == 1

    # A handful of successes should restore useful parallelism, not dozens.
    for _ in range(6):
        c.on_success()
    assert c.limit >= 3, f"expected recovery above 3, got {c.limit}"


def test_aborted_run_releases_waiting_workers():
    """
    Aborting must wake blocked workers, not leave them waiting on a timeout.

    Otherwise a quota abort would still take as long as the slowest wait.
    """
    from visci.pipeline.concurrency import ConcurrencyController

    c = ConcurrencyController(start=1)
    assert c.acquire()

    c.abort("quota reached")
    # A second acquire must refuse immediately rather than block.
    assert c.acquire() is False


def test_jitter_spreads_retries():
    """
    Backoff must be randomised.

    Without jitter, workers that failed together retry together, recreating the
    exact burst that caused the rate limit.
    """
    from visci.pipeline.concurrency import jittered_delay

    delays = {round(jittered_delay(30), 4) for _ in range(50)}
    assert len(delays) > 40, "delays should be spread, not identical"
    assert all(21 <= d <= 39 for d in delays), "jitter should stay near the base"


def test_concurrent_generation_produces_a_complete_film(tmp_path, monkeypatch, providers):
    """
    Concurrency must not corrupt state or lose shots.

    The risk it guards against: several threads calling save() at once would
    interleave writes to state.json — the one file that makes a run resumable.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job("concurrent-test", "a test premise", Mode.DIALOGUE, providers, Format.FILM)
    final = job.run()

    assert final.exists()
    assert all(s.video_path for s in job.state.script.shots), "no shot may be lost"

    # State on disk must be valid JSON, not an interleaved mess.
    from visci.models import JobState

    reloaded = JobState.model_validate_json(
        (tmp_path / "concurrent-test" / "state.json").read_text()
    )
    assert len(reloaded.script.shots) == SHOT_COUNT


def test_quota_error_aborts_the_whole_run(tmp_path, monkeypatch, providers):
    """
    A quota ceiling stops everything rather than failing shot by shot.

    Under concurrency this matters more: without a shared abort, five workers
    each burn their own retry budget discovering the same wall.
    """
    from visci.providers.gemini import QuotaExhausted

    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    calls = []

    def quota_exhausted(shot, bible, keyframe, out_path):
        calls.append(shot.index)
        raise QuotaExhausted("You exceeded your current quota")

    monkeypatch.setattr(providers.video, "generate", quota_exhausted)

    job = Job("quota-test", "a test premise", Mode.DIALOGUE, providers, Format.FILM)
    with pytest.raises(RuntimeError, match="quota"):
        job.run()

    # It must give up quickly rather than attempting all fifteen shots.
    assert len(calls) < SHOT_COUNT, (
        f"quota should abort the run, but {len(calls)} shots were attempted"
    )


# ----------------------------------------------------------------- resume --


def test_empty_premise_is_rejected():
    """
    A film cannot be generated from nothing.

    Without this guard, an empty premise silently produces an arbitrary story
    and bills for it — which is exactly what happened once in development.
    """
    from typer.testing import CliRunner

    from visci.cli import app

    result = CliRunner().invoke(app, ["generate", ""])
    assert result.exit_code == 1
    # Either guard is acceptable — both stop the run before spending.
    assert "No premise" in result.output or "No saved state" in result.output


def test_resume_with_missing_state_fails_loudly(tmp_path, monkeypatch):
    """
    Resuming a job whose state is gone must ERROR, not start a different film.

    The failure mode this prevents: state.json is deleted, the empty premise
    passed for resume is treated as a new job, and a completely unrelated story
    gets generated and charged for.
    """
    from typer.testing import CliRunner

    import visci.cli as cli_module
    from visci.cli import app

    monkeypatch.setattr(cli_module, "settings", cli_module.settings)
    monkeypatch.setattr("visci.config.OUTPUT_DIR", tmp_path)

    result = CliRunner().invoke(app, ["generate", "", "--job-id", "does-not-exist"])
    assert result.exit_code == 1
    assert "No saved state" in result.output or "No premise" in result.output


def test_unicode_escapes_are_decoded():
    """
    Literal escape sequences must become real characters.

    Claude sometimes emits `\\u2014` as six literal characters inside a JSON
    string instead of an em-dash. Left alone it renders as raw `\\u2014` in the
    UI and would be spoken aloud by TTS.
    """
    from pydantic import BaseModel

    from visci.providers.claude import _decode_escapes

    class Nested(BaseModel):
        text: str

    class Doc(BaseModel):
        logline: str
        clean: str
        items: list[Nested]

    doc = Doc(
        logline="a house \\u2014 then silence",
        clean="already fine — untouched",
        items=[Nested(text="caf\\u00e9")],
    )
    _decode_escapes(doc)

    assert doc.logline == "a house — then silence"
    assert doc.clean == "already fine — untouched", "real characters must survive"
    assert doc.items[0].text == "café", "nested models must be decoded too"


def test_story_prompt_teaches_filter_safe_technique():
    """
    The medium brief must tell Claude how to shoot difficult material.

    Preventing a refusal at the writing stage is free; fixing one afterwards
    costs a keyframe plus a clip. This guidance exists because Veo refused a
    held close-up of a woman in visible distress.
    """
    from visci.prompts.story import medium_brief

    lowered = medium_brief(Format.FILM).lower()
    assert "aftermath" in lowered, "should teach shooting aftermath, not the act"
    assert "off-frame" in lowered, "should teach putting contact off-frame"
    # It must NOT tell Claude to avoid difficult subjects — that would produce
    # bland scripts, which is a worse failure than an occasional refusal.
    assert "should not avoid them" in lowered or "not softening" in lowered


# --------------------------------------------------------------- refusals --


def test_refusal_detection_distinguishes_from_transient_errors():
    """
    Refusals and transient faults need opposite handling: retry a network error
    as-is, but reword a refusal. Misclassifying a refusal as transient burns
    money re-requesting something that will never pass.
    """
    from visci.providers.gemini import _looks_like_refusal

    assert _looks_like_refusal("Blocked by safety filters")
    assert _looks_like_refusal("violates content policy")
    assert _looks_like_refusal("PROHIBITED CONTENT")

    assert not _looks_like_refusal("Connection reset by peer")
    assert not _looks_like_refusal("503 Service Unavailable")


def test_transient_server_errors_are_retried_not_fatal():
    """
    A capacity spike must not kill a paid run.

    Uses the real error text Google returned mid-render. These clear on their
    own within minutes, so retrying is correct; a refusal must NOT be retried,
    since only a prompt change fixes that.
    """
    from visci.providers.gemini import _looks_like_refusal, _looks_transient

    overload = (
        "gemini-3-pro-image is currently experiencing high demand, spikes in "
        "demand are usually temporary. Please try again later."
    )
    assert _looks_transient(overload)
    assert not _looks_like_refusal(overload)

    assert _looks_transient("503 Service Unavailable")
    assert _looks_transient("429 RESOURCE_EXHAUSTED")

    # Refusals are permanent — retrying wastes time and money.
    assert not _looks_transient("blocked by safety filters")


def test_transient_retry_gives_up_on_permanent_errors():
    """Retrying must not mask a real bug behind five slow attempts."""
    from visci.providers.base import ContentRefused
    from visci.providers.gemini import _retry_transient

    calls = []

    def always_refused():
        calls.append(1)
        raise ContentRefused("refused")

    with pytest.raises(ContentRefused):
        _retry_transient(always_refused, what="test")
    assert len(calls) == 1, "a refusal must fail on the first attempt"

    def bad_argument():
        calls.append(1)
        raise ValueError("negativePrompt isn't supported by this model")

    calls.clear()
    with pytest.raises(ValueError):
        _retry_transient(bad_argument, what="test")
    assert len(calls) == 1, "a config error must not be retried"


def test_softening_prefix_is_genre_neutral():
    """
    The retry prefix runs for EVERY film — thriller, horror, war, romance. It
    must not assert anything about content, or it will be false for many of them
    and contradict the prompt it is attached to.
    """
    from visci.providers.gemini import SOFTENING_PREFIX

    lowered = SOFTENING_PREFIX.lower()
    for banned in ("domestic", "nothing violent", "no violence", "family"):
        assert banned not in lowered, (
            f"SOFTENING_PREFIX asserts {banned!r} — too specific, it will be "
            f"wrong for other genres"
        )
    # It should still establish the fiction framing that classifiers respond to.
    assert "fiction" in lowered or "scripted" in lowered


def test_sensitive_shots_are_widened_before_generation():
    """
    Sensitive content must never be paired with a tight framing.

    Uses the two shots Veo actually refused in a real run: a close-up of a
    woman concealing a bruise, and a close-up holding suppressed distress.
    Both should be widened to `medium` before a single cent is spent.
    """
    from visci.providers.claude import _widen_sensitive_shots

    shots = [
        Shot(
            index=0,
            character_names=["Dana"],
            location_name="Bathroom",
            framing=Framing.CLOSE_UP,
            camera="85mm, the bruise centred in frame",
            action=(
                "Tight close-up. A diffuse plum-purple bruise sits on her right "
                "cheekbone. She dabs concealer over it."
            ),
            line="I hit it on the cabinet door.",
        ),
        Shot(
            index=1,
            character_names=["Dana"],
            location_name="Street",
            framing=Framing.MEDIUM_CLOSE_UP,
            camera="85mm, head and shoulders",
            action="She is smiling, holding it, eyes not moving. Her lips tremble.",
        ),
        # A safe close-up must be left alone — the rule targets sensitive
        # content, not close-ups in general.
        Shot(
            index=2,
            character_names=["Marcus"],
            location_name="Street",
            framing=Framing.CLOSE_UP,
            camera="50mm, his face high in the tall frame, party lights behind",
            action="He raises a red cup and grins at the crowd.",
            line="Ten years!",
        ),
    ]

    _widen_sensitive_shots(shots)

    assert shots[0].framing is Framing.MEDIUM, "bruise close-up must be widened"
    assert shots[1].framing is Framing.MEDIUM, "distress close-up must be widened"
    assert shots[2].framing is Framing.CLOSE_UP, "safe close-up must be preserved"


def test_prompt_forbids_tight_framing_on_sensitive_content():
    """The shot-list prompt must state the framing rule, not just imply it."""
    from visci.models import Mode, NarrationPerson, StoryBible, Treatment

    bible = StoryBible(
        title="t", logline="l", mode=Mode.DIALOGUE,
        narration_person=NarrationPerson.THIRD,
        characters=[], locations=[],
        visual_style="s", color_palette="p", lens_language="ll", tone="t",
    )
    treatment = Treatment(title="t", logline="l", beats=[])

    from visci.prompts.story import script_prompt

    prompt = script_prompt(bible, treatment, Format.FILM).lower()
    assert "do not use `close_up`" in prompt
    assert "injury" in prompt


def test_wider_framing_ladder_terminates():
    """
    Every widening chain must reach a framing with no fallback.

    A cycle here would retry forever, spending money on each pass.
    """
    from visci.prompts.visual import WIDER_FRAMING

    for start in WIDER_FRAMING:
        seen = {start}
        current = start
        while (current := WIDER_FRAMING.get(current)) is not None:
            assert current not in seen, f"cycle in WIDER_FRAMING from {start}"
            seen.add(current)


def test_refused_close_up_retries_at_wider_framing(tmp_path, monkeypatch, providers):
    """
    A close-up refused by the filter should be retried wider, not abandoned.

    Observed refusals cluster on tight close-ups of a character in distress;
    the same beat at a medium framing renders. This recovers those shots
    automatically instead of leaving a gap.
    """
    from visci.models import Framing
    from visci.providers.base import ContentRefused

    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    real_generate = providers.video.generate
    attempts: list[Framing] = []

    def refuse_close_ups(shot, bible, keyframe, out_path):
        attempts.append(shot.framing)
        if shot.framing is Framing.CLOSE_UP:
            raise ContentRefused("close-up refused")
        return real_generate(shot, bible, keyframe, out_path)

    monkeypatch.setattr(providers.video, "generate", refuse_close_ups)

    job = Job("widen-test", "a test premise", Mode.DIALOGUE, providers, Format.FILM)
    final = job.run()

    assert final.exists()
    # Close-ups were retried wider rather than abandoned.
    assert Framing.MEDIUM_CLOSE_UP in attempts
    assert not job.refused_shots, "widening should have recovered every close-up"

    # Every shot has a clip, so the film is full length.
    assert all(s.video_path for s in job.state.script.shots)


def test_refused_shot_does_not_kill_the_film(tmp_path, monkeypatch, providers):
    """
    THE important one.

    A refused shot must produce a shorter film, not a dead run. Before this was
    handled, one refusal at shot 3 wasted every clip already paid for.
    """
    from visci.providers.base import ContentRefused

    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    real_generate = providers.video.generate

    def refuse_shot_three(shot, bible, keyframe, out_path):
        if shot.index == 3:
            raise ContentRefused("Shot 3 was refused by safety filters.")
        return real_generate(shot, bible, keyframe, out_path)

    monkeypatch.setattr(providers.video, "generate", refuse_shot_three)

    job = Job("refusal-test", "a test premise", Mode.DIALOGUE, providers, Format.FILM)
    final = job.run()

    assert final.exists(), "a refused shot must not prevent assembly"
    assert job.refused_shots == [3]

    refused = job.state.script.shots[3]
    assert refused.refused is True
    assert refused.refusal_reason  # kept so the user can see what to reword

    # The film is exactly one shot shorter, and still a valid video.
    expected = (SHOT_COUNT - 1) * SHOT_DURATION_SECONDS
    assert probe_duration(final) == pytest.approx(expected, abs=1.0)


def test_narration_offsets_shift_when_a_shot_is_refused(tmp_path, monkeypatch, providers):
    """
    Audio offsets must follow position in the ASSEMBLED film, not the original
    shot index. If shot 3 is dropped, shot 4's line belongs at 24s, not 32s —
    otherwise every line after the gap desyncs.
    """
    from visci.providers.base import ContentRefused

    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    real_generate = providers.video.generate

    def refuse_shot_two(shot, bible, keyframe, out_path):
        if shot.index == 2:
            raise ContentRefused("refused")
        return real_generate(shot, bible, keyframe, out_path)

    monkeypatch.setattr(providers.video, "generate", refuse_shot_two)

    job = Job("refusal-narration", "a test premise", Mode.NARRATION, providers, Format.FILM)
    final = job.run()

    assert final.exists()
    expected = (SHOT_COUNT - 1) * SHOT_DURATION_SECONDS
    assert probe_duration(final) == pytest.approx(expected, abs=1.0)


def test_narration_measures_real_audio_duration(tmp_path, monkeypatch, providers):
    """
    Narration duration must be MEASURED, never estimated from word count.

    A line that overruns its 8-second shot desynchronises the film, and the
    drift compounds across 15 shots.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job("narration-test", "a test premise", Mode.NARRATION, providers, Format.FILM)
    job.run()

    spoken = [s for s in job.state.script.shots if s.audio_path]
    assert spoken, "narration mode should produce audio"
    for shot in spoken:
        assert shot.audio_duration_seconds is not None, (
            f"shot {shot.index} has audio but no measured duration"
        )
        assert shot.audio_duration_seconds > 0
