"""
Fake providers. Instant, free, deterministic.

This is what you develop against. A full pipeline run here takes about a second
and costs nothing, so you can iterate on the parts that are genuinely hard —
assembly, retry logic, state handling, the CLI — without waiting 15 minutes or
spending $17 to find out you had a typo in the FFmpeg command.

Three properties are deliberate:

  INSTANT      - no network calls at all, so tests run in milliseconds.
  FREE         - every CostReport is $0.00.
  DETERMINISTIC- the same premise always yields the same story. Randomness in a
                 mock makes failures impossible to reproduce.

The fixture media files are REAL video and audio at the exact dimensions Veo
returns (720x1280, 8 seconds). That matters: assembly code tested against
correctly-shaped input will behave the same when the input becomes real.
"""

import shutil
from pathlib import Path

from visci.config import FIXTURES_DIR
from visci.models import (
    Beat,
    Character,
    Format,
    Framing,
    Location,
    Mode,
    NarrationPerson,
    Script,
    Shot,
    StoryBible,
    Treatment,
)
from visci.providers.base import CostReport

FREE = CostReport(0.0, "mock")

# Camera direction the mock attaches to each shot. Written the way a
# cinematographer would — thinking about the tall vertical frame — so the mock
# produces the same SHAPE of data the real provider does, not a simplified one.
_CAM_LOW = (
    "35mm from just below eye level, the figure in the lower two thirds, "
    "the tower stair spiralling away above her"
)
_CAM_MEDIUM = (
    "50mm at eye level, waist up, centred, the lamp mechanism soft behind "
    "her shoulder"
)
_CAM_CLOSE = (
    "85mm, her face in the upper third of the tall frame, everything past "
    "a foot falling to bokeh"
)
_CAM_INSERT = (
    "100mm macro on her hand at the dial, filling the width, the room dark "
    "above and below"
)
_CAM_MCU = (
    "85mm, head and shoulders low in frame with the ceiling pressing down "
    "above her"
)


class MockStoryProvider:
    """Returns a fixed, coherent little story regardless of the premise."""

    def treatment(
        self, premise: str, mode: Mode, fmt: Format = Format.FILM
    ) -> tuple[Treatment, CostReport]:
        # A SHORT gets a genuinely different beat sheet, not a truncated one —
        # the mock should exercise the same branching the real provider does.
        if fmt is Format.SHORT:
            return (
                Treatment(
                    title="The Last Signal",
                    logline=(
                        "A lighthouse keeper answers a distress call from a "
                        "ship that sank forty years ago."
                    ),
                    beats=[
                        Beat(act=1, summary="The radio crackles in an empty tower.", shot_count=1),
                        Beat(act=1, summary="She recognises the ship's name.", shot_count=1),
                        Beat(act=1, summary="She answers it.", shot_count=1),
                        Beat(act=1, summary="The lamp goes out.", shot_count=1),
                    ],
                ),
                FREE,
            )
        return (
            Treatment(
                title="The Last Signal",
                logline=(
                    "A lighthouse keeper receives a transmission from a ship "
                    "that sank forty years ago."
                ),
                # Beats are sized in SHOTS, not seconds, and must total 15.
                beats=[
                    Beat(act=1, summary="Keeper climbs the tower at dusk, alone.", shot_count=3),
                    Beat(act=1, summary="The radio crackles with an old distress call.", shot_count=2),
                    Beat(act=2, summary="She searches the logbook for the ship's name.", shot_count=3),
                    Beat(act=2, summary="The signal repeats, closer, impossibly.", shot_count=3),
                    Beat(act=3, summary="She answers it, and the light goes out.", shot_count=4),
                ],
            ),
            FREE,
        )

    def bible(
        self, treatment: Treatment, mode: Mode, fmt: Format = Format.FILM
    ) -> tuple[StoryBible, CostReport]:
        return (
            StoryBible(
                title=treatment.title,
                logline=treatment.logline,
                mode=mode,
                narration_person=(
                    NarrationPerson.THIRD if mode is Mode.NARRATION else None
                ),
                narrator_voice_id="Charon" if mode is Mode.NARRATION else None,
                characters=[
                    Character(
                        name="Mara",
                        role="protagonist",
                        face=(
                            "Woman in her early sixties, weathered pale skin, deep "
                            "crow's feet, grey hair pinned back in a loose knot, "
                            "sharp grey-blue eyes, a thin scar through her left eyebrow"
                        ),
                        build=(
                            "Short and solid, slightly stooped shoulders, moves "
                            "deliberately and never hurries"
                        ),
                        wardrobe=(
                            "Heavy oiled navy wool sweater with a frayed collar, "
                            "canvas trousers, scuffed leather boots"
                        ),
                        voice_description=(
                            "Low, dry, unhurried. Faint coastal accent. Long pauses."
                        ),
                    )
                ],
                locations=[
                    Location(
                        name="Lighthouse gallery",
                        description=(
                            "Cramped circular room at the top of a stone tower, "
                            "brass lamp mechanism at centre, salt-fogged windows"
                        ),
                        lighting=(
                            "Failing blue dusk through glass, warm sweeping beam "
                            "from the lamp every four seconds"
                        ),
                    )
                ],
                visual_style=(
                    "Grainy 16mm, desaturated, heavy shadow, practical light only"
                ),
                color_palette=(
                    "Slate blues and cold greys, broken by the warm amber of the lamp"
                ),
                lens_language=(
                    "35mm, shallow focus, static or very slow push. No handheld."
                ),
                tone="Quiet dread, melancholy, restrained",
            ),
            FREE,
        )

    def script(
        self, bible: StoryBible, treatment: Treatment, fmt: Format = Format.FILM
    ) -> tuple[Script, CostReport]:
        # Build exactly as many shots as the beats call for, so the mock exercises
        # the same 15-shot arithmetic the real pipeline uses.
        # Framing paired with the camera direction a cinematographer would
        # actually write for it — the mock should produce the same SHAPE of
        # data as the real provider, not a simplified version.
        coverage = [
            (Framing.LOW_ANGLE_FULL_BODY, _CAM_LOW),
            (Framing.MEDIUM, _CAM_MEDIUM),
            (Framing.CLOSE_UP, _CAM_CLOSE),
            (Framing.INSERT_DETAIL, _CAM_INSERT),
            (Framing.MEDIUM_CLOSE_UP, _CAM_MCU),
        ]
        shots: list[Shot] = []
        index = 0
        for beat in treatment.beats:
            for _ in range(beat.shot_count):
                shots.append(
                    Shot(
                        index=index,
                        character_names=["Mara"],
                        location_name="Lighthouse gallery",
                        framing=coverage[index % len(coverage)][0],
                        camera=coverage[index % len(coverage)][1],
                        action=beat.summary,
                        line=f"Mock narration line for shot {index}.",
                        speaker="Mara" if bible.mode is Mode.DIALOGUE else None,
                    )
                )
                index += 1
        return Script(bible=bible, shots=shots), FREE


class MockImageProvider:
    """Copies a fixture PNG wherever an image is requested."""

    def reference_sheet(
        self, character: Character, bible: StoryBible, out_dir: Path
    ) -> tuple[list[Path], CostReport]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        # Four angles, matching what the real provider produces.
        for angle in ("front", "three_quarter", "profile", "full_body"):
            dest = out_dir / f"{character.name.lower()}_{angle}.png"
            shutil.copy(FIXTURES_DIR / "mock_keyframe.png", dest)
            paths.append(dest)
        return paths, FREE

    def keyframe(
        self, shot: Shot, bible: StoryBible, out_path: Path
    ) -> tuple[Path, CostReport]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES_DIR / "mock_keyframe.png", out_path)
        return out_path, FREE


class MockVideoProvider:
    """Copies a real 720x1280 8-second clip wherever a video is requested."""

    def generate(
        self, shot: Shot, bible: StoryBible, keyframe: Path, out_path: Path
    ) -> tuple[Path, CostReport]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES_DIR / "mock_clip.mp4", out_path)
        return out_path, FREE


class MockVoiceProvider:
    """Copies a fixture audio file wherever narration is requested."""

    def speak(
        self, text: str, voice_id: str, out_path: Path
    ) -> tuple[Path, CostReport]:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES_DIR / "mock_voice.m4a", out_path)
        return out_path, FREE
