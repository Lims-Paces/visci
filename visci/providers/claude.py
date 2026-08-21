"""
The real Claude provider. Costs money.

This is where the Pydantic models earn their keep. We hand Claude a model class
and get back a validated instance — no JSON parsing, no "did it include all the
fields", no defensive checks. If Claude's reply doesn't fit the schema, the SDK
raises immediately rather than letting a malformed bible flow downstream and
fail confusingly five stages later.

COST
----
All three stages together are roughly $0.60 per film — about 3.5% of the total.
Video is ~88%. So this is the cheap part, and it is worth spending tokens here
(high effort, adaptive thinking) to get a better script, because script quality
determines everything downstream. A weak shot list produces a weak film no
matter how good the video model is.
"""

from anthropic import Anthropic
from pydantic import BaseModel

from visci.config import settings
from visci.ledger import (
    CLAUDE_INPUT_USD_PER_TOKEN,
    CLAUDE_OUTPUT_USD_PER_TOKEN,
)
from visci.models import (
    SHOT_COUNT,
    Framing,
    Mode,
    Script,
    Shot,
    StoryBible,
    Treatment,
)
from visci.prompts.story import (
    bible_prompt,
    script_prompt,
    treatment_prompt,
)
from visci.providers.base import CostReport


class ShotList(BaseModel):
    """
    Wrapper for the shot-list response.

    Claude returns just the shots; we attach the bible ourselves afterwards.
    Asking it to echo the entire bible back would waste output tokens on data we
    already have — and risk it "helpfully" altering a frozen description.
    """

    shots: list[Shot]


class ClaudeStoryProvider:
    """Generates treatment, story bible, and shot list via the Claude API."""

    def __init__(self) -> None:
        key = (settings.anthropic_api_key or "").strip()

        # Validate the key SHAPE before making any network call. Without this,
        # an empty or placeholder value produces a long retry-and-backoff hang
        # instead of an immediate, obvious error — which is a miserable way to
        # discover you forgot to paste the key.
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is empty.\n"
                "Add it to .env:  ANTHROPIC_API_KEY=sk-ant-...\n"
                "Get one at https://console.anthropic.com/settings/keys"
            )
        if not key.startswith("sk-ant-"):
            raise RuntimeError(
                f"ANTHROPIC_API_KEY does not look like a real key "
                f"(starts with {key[:7]!r}, expected 'sk-ant-').\n"
                "Check you pasted the whole key and not a placeholder."
            )

        self.client = Anthropic(
            api_key=key,
            # Fail in 2 minutes rather than the 10-minute default. These calls
            # normally take 10-40 seconds; anything past 2 minutes is a problem
            # you want to hear about promptly.
            timeout=120.0,
            max_retries=2,
        )
        self.model = settings.claude_model

    # ------------------------------------------------------------ internals --

    def _cost(self, response, label: str) -> CostReport:
        """
        Work out what a call cost from the token counts the API reports.

        Note this uses ACTUAL usage returned by the API, not an estimate. The
        per-token prices are published constants that can go stale, but the
        token counts are real.
        """
        usd = (
            response.usage.input_tokens * CLAUDE_INPUT_USD_PER_TOKEN
            + response.usage.output_tokens * CLAUDE_OUTPUT_USD_PER_TOKEN
        )
        return CostReport(usd, f"claude:{label}")

    def _parse(self, prompt: str, schema: type, label: str, effort: str = "high"):
        """
        One structured call to Claude.

        `output_format=<PydanticModel>` is the key line: it constrains Claude to
        reply in exactly that shape, and `.parsed_output` comes back as a
        validated instance of the model.

        We use streaming because these are long generations — a full shot list
        can run to several thousand tokens, and non-streaming requests risk
        hitting HTTP timeouts.
        """
        with self.client.messages.stream(
            model=self.model,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            messages=[{"role": "user", "content": prompt}],
            output_format=schema,
        ) as stream:
            response = stream.get_final_message()

        parsed = response.parsed_output
        _decode_escapes(parsed)
        return parsed, self._cost(response, label)

    # -------------------------------------------------------------- stages --

    def treatment(self, premise: str, mode: Mode) -> tuple[Treatment, CostReport]:
        """Stage 1: premise -> three-act beat sheet, sized in shots."""
        treatment, cost = self._parse(
            treatment_prompt(premise, mode), Treatment, "treatment"
        )

        # Claude is asked for beats totalling exactly SHOT_COUNT, but a schema
        # cannot enforce a sum. Check it here rather than discovering the film
        # is the wrong length after generating video.
        total = sum(b.shot_count for b in treatment.beats)
        if total != SHOT_COUNT:
            treatment = self._rebalance(treatment, total)

        return treatment, cost

    def _rebalance(self, treatment: Treatment, total: int) -> Treatment:
        """
        Nudge beat shot-counts so they total exactly SHOT_COUNT.

        Rather than retrying the whole call (which costs money and may drift
        again), we adjust the largest beats. The story is unaffected — a beat
        going from 4 shots to 3 loses no content, it just gets tighter.
        """
        difference = SHOT_COUNT - total
        beats = sorted(treatment.beats, key=lambda b: b.shot_count, reverse=True)

        while difference != 0:
            for beat in beats:
                if difference == 0:
                    break
                if difference > 0:
                    beat.shot_count += 1
                    difference -= 1
                elif beat.shot_count > 1:  # never drop a beat to zero
                    beat.shot_count -= 1
                    difference += 1

        return treatment

    def bible(self, treatment: Treatment, mode: Mode) -> tuple[StoryBible, CostReport]:
        """
        Stage 2: treatment -> characters, locations, visual grammar.

        Highest effort of the three stages. This document is frozen and quoted
        verbatim into every downstream prompt — vague character descriptions
        here become drifting faces on screen, which is the single most visible
        failure mode of the whole product.
        """
        bible, cost = self._parse(
            bible_prompt(treatment, mode), StoryBible, "bible", effort="xhigh"
        )
        # The schema has a `mode` field but Claude has no reason to get it
        # right — we know the answer, so set it authoritatively.
        bible.mode = mode
        if mode is Mode.NARRATION and not bible.narrator_voice_id:
            bible.narrator_voice_id = settings.default_narrator_voice
        return bible, cost

    def script(
        self, bible: StoryBible, treatment: Treatment
    ) -> tuple[Script, CostReport]:
        """Stage 3: bible + treatment -> the 15-shot list."""
        shot_list, cost = self._parse(
            script_prompt(bible, treatment), ShotList, "script"
        )

        shots = shot_list.shots

        # Indices matter — they determine playback order and the audio offsets
        # used when mixing narration. Claude usually gets these right, but
        # "usually" is not good enough for something this load-bearing.
        for position, shot in enumerate(shots):
            shot.index = position

        if len(shots) != SHOT_COUNT:
            raise ValueError(
                f"Expected {SHOT_COUNT} shots, got {len(shots)}. "
                f"The film would be {len(shots) * 8}s instead of 120s."
            )

        _widen_sensitive_shots(shots)

        return Script(bible=bible, shots=shots), cost


def _decode_escapes(model: BaseModel) -> None:
    """
    Turn literal escape sequences in generated text into real characters.

    Claude sometimes emits `\\u2014` as six literal characters inside a JSON
    string rather than as an em-dash. The SDK documents this: models may vary
    their unicode escaping in structured output. The result renders as raw
    `\\u2014` in the UI and would end up spoken aloud by TTS.

    Fixed here at the boundary — once, where text enters the system — rather
    than in every consumer.
    """
    for name in type(model).model_fields:
        value = getattr(model, name, None)

        if isinstance(value, str) and "\\u" in value:
            try:
                setattr(model, name, value.encode().decode("unicode_escape"))
            except (UnicodeDecodeError, AttributeError):
                pass  # leave the original rather than mangling it
        elif isinstance(value, BaseModel):
            _decode_escapes(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, BaseModel):
                    _decode_escapes(item)


# Words that suggest a shot depicts harm, injury, or acute distress. Matched
# against the action and line text.
#
# Deliberately broad — a false positive costs nothing but a slightly wider
# shot, while a false negative costs a refused generation plus a wasted
# keyframe. Erring toward wide is the cheap direction.
_SENSITIVE_MARKERS = (
    "bruise", "blood", "bleed", "wound", "injur", "scar", "swollen", "welt",
    "cut", "burn", "mark on", "conceal", "cover up", "hide the",
    "flinch", "cower", "cringe", "tremble", "shaking", "sob", "weep",
    "cry", "tears", "terrified", "afraid", "fear", "panic", "grab",
    "strike", "hit", "slap", "punch", "shove", "push", "choke", "drag",
)

# Framings too tight for sensitive content, and what to use instead.
_WIDEN_FOR_SENSITIVE = {
    Framing.CLOSE_UP: Framing.MEDIUM,
    Framing.MEDIUM_CLOSE_UP: Framing.MEDIUM,
}


def _widen_sensitive_shots(shots: list[Shot]) -> None:
    """
    Force sensitive shots to a wider framing before they are ever generated.

    WHY THIS EXISTS
    ---------------
    Observed refusals cluster hard on TIGHT CLOSE-UPS whose subject is injury
    or distress. A close-up fills the frame with one thing, so the sensitive
    element becomes the entire image — which is precisely what a content filter
    is built to catch.

    The prompt already instructs Claude to avoid this pairing, but a prompt is
    guidance, not a guarantee. This is the backstop, and it runs BEFORE any
    money is spent: widening here is free, whereas discovering the problem at
    generation time costs a wasted keyframe plus a refused clip.

    It is also usually the better shot. A woman covering a bruise, seen in a
    medium shot with the room around her, carries more than the same moment
    shot tight — you see the ordinary world she has to keep performing in.
    """
    for shot in shots:
        wider = _WIDEN_FOR_SENSITIVE.get(shot.framing)
        if wider is None:
            continue

        text = f"{shot.action} {shot.line or ''}".lower()
        if any(marker in text for marker in _SENSITIVE_MARKERS):
            shot.framing = wider
