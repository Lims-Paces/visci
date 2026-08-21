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
TARGET_RUNTIME_SECONDS = 120
SHOT_COUNT = TARGET_RUNTIME_SECONDS // SHOT_DURATION_SECONDS  # 15
ASPECT_RATIO = "9:16"
RESOLUTION = "720p"

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

    # Set later, once we assign a real ElevenLabs voice. Optional because it
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

    framing: Framing
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


class Script(BaseModel):
    """The full shot list — the film, fully specified but not yet rendered."""

    bible: StoryBible
    shots: list[Shot]

    def runtime_seconds(self) -> int:
        """Total runtime. Should always be 120 for v1."""
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
