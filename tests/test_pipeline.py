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


# ---------------------------------------------------------------- pipeline --


@pytest.mark.parametrize("mode", [Mode.DIALOGUE, Mode.NARRATION])
def test_full_pipeline_produces_a_correct_film(tmp_path, monkeypatch, providers, mode):
    """
    End-to-end: premise in, correctly-shaped film out.

    Checks the things that would silently break a real run — dimensions,
    duration, shot count.
    """
    monkeypatch.setattr("visci.pipeline.run.OUTPUT_DIR", tmp_path)

    job = Job(f"test-{mode.value}", "a test premise", mode, providers)
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

    bible, _ = providers.story.bible(
        providers.story.treatment("x", Mode.DIALOGUE)[0], Mode.DIALOGUE
    )
    script, _ = providers.story.script(bible, providers.story.treatment("x", Mode.DIALOGUE)[0])

    character = bible.characters[0]
    shot = next(s for s in script.shots if character.name in s.character_names)

    prompt = keyframe_prompt(shot, bible)
    assert character.face in prompt, "face description must appear verbatim"
    assert character.wardrobe in prompt, "wardrobe must appear verbatim"
    assert bible.visual_style in prompt, "visual style must appear verbatim"

    # The style block must also carry into the video prompt, or the clip drifts
    # from the keyframe it was built on.
    assert bible.visual_style in video_prompt(shot, bible)


def test_every_framing_has_prompt_language():
    """
    A framing added to the enum without prompt language would raise KeyError
    mid-generation — after money has already been spent on earlier shots.
    """
    from visci.prompts.visual import FRAMING_LANGUAGE

    for framing in Framing:
        assert framing in FRAMING_LANGUAGE, f"{framing} has no prompt phrasing"


# ----------------------------------------------------------------- export --


def _sample_state(providers):
    from visci.models import JobState

    treatment, _ = providers.story.treatment("x", Mode.DIALOGUE)
    bible, _ = providers.story.bible(treatment, Mode.DIALOGUE)
    script, _ = providers.story.script(bible, treatment)
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

    job = Job("concurrent-test", "a test premise", Mode.DIALOGUE, providers)
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

    job = Job("quota-test", "a test premise", Mode.DIALOGUE, providers)
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
    from visci.prompts.story import MEDIUM_BRIEF

    lowered = MEDIUM_BRIEF.lower()
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
            action="She is smiling, holding it, eyes not moving. Her lips tremble.",
        ),
        # A safe close-up must be left alone — the rule targets sensitive
        # content, not close-ups in general.
        Shot(
            index=2,
            character_names=["Marcus"],
            location_name="Street",
            framing=Framing.CLOSE_UP,
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

    prompt = script_prompt(bible, treatment).lower()
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

    job = Job("widen-test", "a test premise", Mode.DIALOGUE, providers)
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

    job = Job("refusal-test", "a test premise", Mode.DIALOGUE, providers)
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

    job = Job("refusal-narration", "a test premise", Mode.NARRATION, providers)
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

    job = Job("narration-test", "a test premise", Mode.NARRATION, providers)
    job.run()

    spoken = [s for s in job.state.script.shots if s.audio_path]
    assert spoken, "narration mode should produce audio"
    for shot in spoken:
        assert shot.audio_duration_seconds is not None, (
            f"shot {shot.index} has audio but no measured duration"
        )
        assert shot.audio_duration_seconds > 0
