"""
The shapes of our data.

Every structure that crosses a boundary — Claude's response, a file written to
disk, a job's saved state — is defined here as a Pydantic model.

Why Pydantic: it does two jobs at once.
  1. It documents and type-checks our data, so the editor autocompletes and
     catches mistakes before you run anything.
  2. It generates a JSON Schema, which we hand to Claude as a structured-output
     definition. Claude is then *required* to reply in exactly this shape.

That second point is the big one. Without it we'd be parsing free-form text and
hoping. With it, if Claude's reply doesn't fit the model, we find out
immediately with a clear error instead of a mysterious failure five stages later.
"""

from enum import Enum

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Locked v1 constraints.
#
# These live in code as constants, not as settings, because they are not
# configurable — they are dictated by the Veo API. `durationSeconds` accepts
# only 4, 6, or 8, and MUST be 8 when using reference images or first/last-frame
# interpolation. Both are core to our consistency strategy, so 8 is fixed.
# ---------------------------------------------------------------------------

SHOT_DURATION_SECONDS = 8
ASPECT_RATIO = "9:16"
RESOLUTION = "720p"


class Format(str, Enum):
    """
    How long a film is — and therefore what kind of story it can tell.

    This is NOT merely a duration setting. Four shots is not a short version of
    fifteen; it is a different form:

      SHORT  4 shots.  One image, one beat, one hit. No room for a three-act
             structure — attempting one produces incoherence. Think of a single
             charged moment: the thing that happens, and its consequence.

      FILM   15 shots. Setup, complication, turn. A real three-act shape with
             space for a reveal in the closing shots.

    So the story prompts branch on this, not just the arithmetic.

    WHY THESE TWO AND NOT ARBITRARY LENGTHS
    ---------------------------------------
    Veo accepts only 4, 6, or 8 second clips, and REQUIRES 8 when using
    reference images — which we depend on for character consistency. So runtime
    can only move in 8-second steps, and "30 seconds" is really 4 shots = 32s.

    More importantly, an arbitrary length means the story prompt cannot know
    what structure to write for, and you get muddled pacing at every duration.
    Two well-tuned formats beat infinite badly-tuned ones.
    """

    SHORT = "short"
    FILM = "film"

    @property
    def shot_count(self) -> int:
        return 4 if self is Format.SHORT else 15

    @property
    def runtime_seconds(self) -> int:
        """Actual runtime. SHORT is 32s, which we present to users as ~30s."""
        return self.shot_count * SHOT_DURATION_SECONDS

    @property
    def label(self) -> str:
        """What the user sees. Rounded, because 32 is a technical artefact."""
        return "30 seconds" if self is Format.SHORT else "2 minutes"


# The default. A 30-second short costs roughly a third of a full film, which
# makes it both the sensible starting point and a far better free tier.
DEFAULT_FORMAT = Format.SHORT

# Retained for the many call sites that predate multiple formats. Equal to the
# FILM format so existing behaviour is unchanged where it is still used.
TARGET_RUNTIME_SECONDS = Format.FILM.runtime_seconds
SHOT_COUNT = Format.FILM.shot_count

# ---------------------------------------------------------------------------
# Narration pacing.
#
# IMPORTANT: this is a *drafting guide*, never a prediction.
#
# ~145 words per minute is the standard documentary narration rate, so ~19 words
# is a reasonable target for an 8-second shot. We put that number in the prompt
# so Claude doesn't write 80 words for a shot that can hold 19.
#
# But real spoken duration depends on the specific voice, its speaking rate,
# punctuation, and pauses — none of which this constant knows about. So we NEVER
# trust it. After synthesising audio we measure the actual duration with FFmpeg
# (see pipeline/audio.py) and reconcile any overrun.
#
# Why this matters: video shots are locked at exactly 8 seconds, but audio is
# not. A line that takes 9.4 seconds to speak overruns its shot. Fifteen shots
# of small drift and the voice-over desynchronises from the picture.
#
# Note this only applies to NARRATION mode. In DIALOGUE mode, Veo generates
# speech natively inside the clip, so it is inherently within the 8 seconds and
# these numbers are irrelevant.
# ---------------------------------------------------------------------------

NARRATION_WORDS_PER_MINUTE = 145  # drafting guide only — always measure the result
NARRATION_WORDS_PER_SHOT_GUIDE = (NARRATION_WORDS_PER_MINUTE * SHOT_DURATION_SECONDS) // 60  # ~19

# How much a synthesised line may exceed its shot before we treat it as a
# problem. A little overrun is absorbable by trimming trailing silence; beyond
# this we regenerate the line shorter.
NARRATION_OVERRUN_TOLERANCE_SECONDS = 0.4


class Mode(str, Enum):
    """The two kinds of film we can make."""

    DIALOGUE = "dialogue"
    NARRATION = "narration"


class NarrationPerson(str, Enum):
    """
    Grammatical person of the narrator, for NARRATION mode.

    This is a real storytelling choice, not a technical one, and it changes how
    Claude writes every line:

      FIRST  - "I walked into the room." The narrator is a character in the
               story. Intimate, unreliable, past-tense memoir feel.
      SECOND - "You walk into the room." Rare and striking. Puts the viewer in
               the story. Works well for short, tense pieces.
      THIRD  - "She walked into the room." The classic documentary or fable
               voice. Observational, flexible, the safe default.

    One voice carries the entire film regardless of which is chosen — the person
    affects the writing, not the casting.
    """

    FIRST = "first"
    SECOND = "second"
    THIRD = "third"


class Framing(str, Enum):
    """
    Allowed camera framings.

    This is deliberately restrictive. A 9:16 vertical frame physically cannot
    hold a wide group shot — ask for one and the model produces mush. By making
    framing an enum, Claude *cannot* request a framing we know will fail. The
    constraint is enforced by the schema rather than by hoping the prompt is
    obeyed.
    """

    CLOSE_UP = "close_up"
    MEDIUM_CLOSE_UP = "medium_close_up"
    MEDIUM = "medium"
    OVER_THE_SHOULDER = "over_the_shoulder"
    LOW_ANGLE_FULL_BODY = "low_angle_full_body"
    HIGH_ANGLE = "high_angle"
    INSERT_DETAIL = "insert_detail"


class Character(BaseModel):
    """
    One character, described exhaustively.

    The descriptions here are verbatim-injected into every image and video
    prompt this character appears in. They must be specific enough that two
    separate generations produce the same person. "A young woman" will not do;
    "a woman in her late twenties, sharp jawline, dark brown eyes, black hair
    in a blunt shoulder-length bob" will.
    """

    name: str
    role: str = Field(description="Their function in the story, e.g. protagonist")

    # Physical description is split into fields rather than one blob so each
    # part can be reused independently — face for close-ups, wardrobe for
    # full-body shots.
    face: str = Field(description="Face, hair, eyes, skin, distinguishing features")
    build: str = Field(description="Height, build, posture, how they carry themselves")
    wardrobe: str = Field(description="Exact clothing, worn consistently throughout")

    voice_description: str = Field(description="Age, accent, pitch, pace, texture")

    # Set later, once we assign a narrator voice (Gemini TTS). Optional because it
    # doesn't exist at the time Claude writes the bible.
    voice_id: str | None = None

    # Paths to the generated reference images. Populated in stage 5.
    reference_image_paths: list[str] = Field(default_factory=list)


class Location(BaseModel):
    """A place the film visits."""

    name: str
    description: str = Field(description="Architecture, textures, scale, contents")
    lighting: str = Field(description="Source, direction, colour temperature, mood")


class StoryBible(BaseModel):
    """
    The single source of truth for the entire film.

    Generated once at the start, then frozen. Every subsequent prompt quotes
    from this verbatim. This artifact is the difference between a film and
    fifteen unrelated clips — if character descriptions drift between prompts,
    they drift on screen.
    """

    title: str
    logline: str = Field(description="One sentence: who wants what, and what stops them")
    mode: Mode

    # Only meaningful in NARRATION mode; None for dialogue films.
    narration_person: NarrationPerson | None = None

    # The single voice that narrates the whole film. Chosen once, used for every
    # line. None for dialogue films, where Veo generates speech per character.
    narrator_voice_id: str | None = None

    characters: list[Character]
    locations: list[Location]

    # Global visual grammar, applied to every single shot.
    visual_style: str = Field(description="Film stock, grade, contrast, era, references")
    color_palette: str = Field(description="Dominant colours and their emotional role")
    lens_language: str = Field(description="Focal lengths, depth of field, camera movement")
    tone: str = Field(description="Emotional register of the piece")


class Beat(BaseModel):
    """
    One story beat, sized in shots.

    Note the unit: shots, not seconds. Because shot length is locked at 8
    seconds by the API, asking Claude for "20 seconds" invites answers that
    don't divide evenly. Asking for "3 shots" cannot go wrong.
    """

    act: int = Field(ge=1, le=3)
    summary: str
    shot_count: int = Field(ge=1, description="How many 8-second shots this beat needs")


class Treatment(BaseModel):
    """The story plan, before any script is written."""

    title: str
    logline: str
    beats: list[Beat]


class Shot(BaseModel):
    """
    One 8-second unit of film. The atomic unit of the whole pipeline.

    Everything needed to generate this shot in isolation lives here, because
    that is exactly how it will be generated — the video model has no memory of
    the previous shot.
    """

    index: int = Field(ge=0, description="Position in the film, starting at 0")

    # Which characters are on screen. We keep this to at most two — a vertical
    # frame cannot hold more without the model losing track of faces.
    character_names: list[str] = Field(max_length=2)
    location_name: str

    # The coarse category. Kept because two mechanical systems need a KNOWN
    # value to reason about: the sensitivity check that widens risky shots, and
    # the refusal-recovery ladder that steps a refused shot to the next-wider
    # framing. Neither can work on free text.
    framing: Framing = Field(
        description="The broad shot size. Used for safety handling."
    )

    # The actual camera direction, written per shot.
    #
    # WHY CLAUDE WRITES THIS AND NOT THE VIDEO MODEL
    # ----------------------------------------------
    # An earlier version reduced this to a vague hint on the theory that Veo
    # "has seen more cinema" than a seven-value enum. That was the wrong way
    # round: Veo sees ONE keyframe and one paragraph. Claude knows the whole
    # film — the shot before, the shot after, that this character is concealing
    # an injury, that the antagonist dominates public space and shrinks private
    # ones.
    #
    # Claude also knows the frame is 9:16, which is not a category lookup but a
    # judgement per shot. Two people in a vertical frame needs a specific
    # solution — stacked depth, one soft in the foreground, a mirror, an
    # over-the-shoulder — and an enum cannot express any of those. Claude can.
    #
    # So the model with more context does the composing, and we pass what it
    # writes through verbatim rather than diluting it.
    camera: str = Field(
        description=(
            "How this shot is covered: lens, angle, what sits where in the "
            "tall vertical frame, what is sharp and what falls off."
        )
    )

    action: str = Field(description="What physically happens, in visual terms only")

    # The line spoken during this shot. For narration mode this is voice-over;
    # for dialogue mode it is spoken on camera by a character.
    line: str | None = None
    speaker: str | None = Field(default=None, description="Character name, if dialogue")

    # Populated as the pipeline runs. These start empty and fill in stage by
    # stage, which is what lets us resume a half-finished job later.
    keyframe_path: str | None = None
    video_path: str | None = None
    audio_path: str | None = None

    # The MEASURED duration of the synthesised narration, in seconds — filled in
    # by FFmpeg after the audio exists. Compared against SHOT_DURATION_SECONDS
    # to detect overruns. Never estimated from word count.
    audio_duration_seconds: float | None = None

    # Set when the video model declines to generate this shot on safety grounds.
    # The shot stays in the script — we do not delete it — so you can see what
    # was refused, reword it, and re-run to fill the gap.
    refused: bool = False
    refusal_reason: str | None = None


# Words suggesting a shot depicts harm, injury, or acute distress.
#
# Deliberately broad — a false positive costs only a slightly wider shot, while
# a false negative costs a refused generation plus a wasted keyframe. Erring
# toward wide is the cheap direction.
#
# Lives here rather than in a provider so both the prompt layer and the script
# validator can ask the same question and get the same answer.
_SENSITIVE_MARKERS = (
    "bruise", "blood", "bleed", "wound", "injur", "scar", "swollen", "welt",
    "cut", "burn", "mark on", "conceal", "cover up", "hide the",
    "flinch", "cower", "cringe", "tremble", "shaking", "sob", "weep",
    "cry", "tears", "terrified", "afraid", "fear", "panic", "grab",
    "strike", "hit", "slap", "punch", "shove", "push", "choke", "drag",
)


def is_sensitive(shot: "Shot") -> bool:
    """
    Whether this shot's content risks a safety refusal.

    Sensitive shots keep a prescribed frame; everything else lets the video
    model choose its own coverage, which produces more natural results.
    """
    text = f"{shot.action} {shot.line or ''}".lower()
    return any(marker in text for marker in _SENSITIVE_MARKERS)


class Script(BaseModel):
    """The full shot list — the film, fully specified but not yet rendered."""

    bible: StoryBible
    shots: list[Shot]

    def runtime_seconds(self) -> int:
        """Total runtime, derived from the shots actually present."""
        return len(self.shots) * SHOT_DURATION_SECONDS


class JobState(BaseModel):
    """
    Everything about one film-generation run, saved to disk after every stage.

    This is what makes the pipeline resumable. If shot 12 of 15 fails, we
    reload this file and continue from shot 12 rather than restarting — which
    matters a great deal when restarting costs $17.
    """

    job_id: str
    premise: str
    mode: Mode

    # Which format this film is. Stored on the job because every later stage
    # needs it — the treatment prompt branches on structure, the shot list needs
    # a count, and the cost estimate scales with it.
    format: Format = DEFAULT_FORMAT

    # Whether this job's images and video are real (paid) or mock, and which
    # video tier. Recorded so a resume continues the SAME way the job started:
    # resuming a free preview must never silently switch to paid generation,
    # and resuming a real film with mocks would splice placeholder clips into
    # it. None means the job was created before this was recorded.
    real: bool | None = None
    draft: bool | None = None

    treatment: Treatment | None = None
    bible: StoryBible | None = None
    script: Script | None = None
    final_video_path: str | None = None

    # Running total of real money spent, in US dollars. Updated by every
    # provider call. This is how the spend ceiling gets enforced.
    cost_usd: float = 0.0

    # Which prompt version produced this job. Stored so that when we improve a
    # prompt later, we can tell which films came from which version — otherwise
    # you cannot explain why last month's output looked different.
    prompt_version: str = "v1"

    # Set when a run fails. Persisted so the web UI can show what went wrong
    # after the fact — a background thread has no other way to report to a
    # client that polls.
    error: str | None = None

    def uses_real_media(self) -> bool:
        """Whether resuming this job would spend money on images and video."""
        if self.real is not None:
            return self.real
        # Older jobs did not record it. Real media costs dollars per shot, while
        # a real-script-only run costs well under one, so spend tells them apart.
        return self.cost_usd > 1.0
