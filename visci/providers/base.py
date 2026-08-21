"""
Provider interfaces — the contracts every external service must satisfy.

This is the most important architectural decision in the project, so it is worth
understanding rather than skimming.

THE IDEA
--------
We never call Veo, Claude, or the TTS API directly from pipeline code. Instead we
define here *what* each kind of provider does — "given a prompt and a keyframe,
give me back a video file" — without saying *how*. Then we write two
implementations of each:

    real.py  - actually calls the API, costs real money, takes real minutes
    mock.py  - returns fixture files instantly, costs nothing

The pipeline code cannot tell them apart. It just calls `video.generate(...)`.

WHY THIS MATTERS — four concrete payoffs
----------------------------------------
1. DEVELOPMENT IS FREE AND FAST. A full pipeline run in mock mode takes about a
   second and costs $0. Without this, every test of your FFmpeg assembly code
   would cost $17 and 15 minutes. Over an 8-week build that is the difference
   between ~$200 and ~$1,300 in spend, and between iterating in seconds versus
   iterating in quarter-hours.

2. PROVIDER SWAPS ARE CONTAINED. Veo model IDs carry a `-preview` suffix and
   will change at GA. Sora 2's API is being removed entirely. When — not if —
   this happens, you write one new class instead of hunting call sites.

3. COST TRACKING HAPPENS IN ONE PLACE. Every provider reports what it spent, so
   the ledger cannot drift from reality.

4. TESTS ARE POSSIBLE AT ALL. You cannot write a test that spends money.

Python's `Protocol` is how we express "anything with these methods counts" —
similar to a TypeScript interface. A class does not need to inherit from it; it
just needs matching methods.
"""

from pathlib import Path
from typing import Protocol

from visci.models import Character, Mode, Script, Shot, StoryBible, Treatment


class ContentRefused(RuntimeError):
    """
    A provider declined to generate this content on safety grounds.

    Lives here rather than in a specific provider module so the orchestrator can
    catch it without importing any vendor SDK — a mock-mode run should never
    need the Gemini library loaded.

    Handled differently to other failures: a network error should be retried
    as-is, but a refusal will be refused again unless the prompt changes. And
    critically, a refusal is NOT a reason to abandon the film — the other
    fourteen shots are still viable.
    """


class CostReport:
    """
    What one provider call cost.

    Every provider returns one of these so spend can be totalled centrally
    rather than estimated. Note the principle from earlier: we ESTIMATE to plan,
    but we record what was ACTUALLY billed wherever the API tells us.
    """

    def __init__(self, usd: float, detail: str = "") -> None:
        self.usd = usd
        self.detail = detail

    def __repr__(self) -> str:
        return f"CostReport(${self.usd:.4f}, {self.detail!r})"


class StoryProvider(Protocol):
    """
    Turns text into structured story data. Backed by Claude.

    Three separate calls rather than one, because each stage locks decisions the
    next depends on — and because a failure in shot-listing shouldn't force
    regeneration of the treatment.
    """

    def treatment(self, premise: str, mode: Mode) -> tuple[Treatment, CostReport]:
        """Premise -> three-act beat sheet, sized in shots."""
        ...

    def bible(self, treatment: Treatment, mode: Mode) -> tuple[StoryBible, CostReport]:
        """Treatment -> characters, locations, and visual grammar. Frozen after this."""
        ...

    def script(self, bible: StoryBible, treatment: Treatment) -> tuple[Script, CostReport]:
        """Bible + treatment -> the full 15-shot list."""
        ...


class ImageProvider(Protocol):
    """
    Generates still images. Backed by Gemini's Nano Banana Pro.

    Used for two distinct jobs, both essential to character consistency:
      - character reference sheets (multi-angle stills, generated once)
      - per-shot keyframes (the first frame of each shot)

    Keyframes are the highest-leverage step in the pipeline. Generating a still
    and then animating it converts an unreliable text-to-video call into a much
    more reliable image-to-video one — and stills cost ~40x less to iterate.
    """

    def reference_sheet(
        self, character: Character, bible: StoryBible, out_dir: Path
    ) -> tuple[list[Path], CostReport]:
        """Generate multi-angle reference stills for one character."""
        ...

    def keyframe(
        self, shot: Shot, bible: StoryBible, out_path: Path
    ) -> tuple[Path, CostReport]:
        """Compose the opening frame of one shot, in 9:16."""
        ...


class VideoProvider(Protocol):
    """
    Turns a keyframe into an 8-second clip. Backed by Veo 3.1 Fast.

    Locked parameters for v1: 9:16, 720p, exactly 8 seconds. The duration is not
    a choice — the Veo API accepts only 4, 6, or 8, and REQUIRES 8 when using
    reference images or first/last-frame interpolation, both of which we depend
    on for consistency.
    """

    def generate(
        self, shot: Shot, bible: StoryBible, keyframe: Path, out_path: Path
    ) -> tuple[Path, CostReport]:
        """Keyframe + shot description -> an 8-second 9:16 clip."""
        ...


class VoiceProvider(Protocol):
    """
    Text -> spoken audio. Backed by Gemini Flash TTS.

    ONLY used in NARRATION mode. In dialogue mode, Veo generates lip-synced
    speech natively inside the clip, so no TTS is involved at all.

    A single narrator voice carries the entire film, so this is a much smaller
    problem than per-character casting — which is why we don't need ElevenLabs'
    voice-cloning strength and can use the Gemini key we already hold.
    """

    def speak(
        self, text: str, voice_id: str, out_path: Path
    ) -> tuple[Path, CostReport]:
        """Synthesise one narration line to an audio file."""
        ...


class Providers:
    """
    The full set of providers for one run, bundled together.

    Pipeline stages receive this object rather than reaching for globals, which
    is what makes it trivial to hand them mock implementations in tests.
    """

    def __init__(
        self,
        story: StoryProvider,
        image: ImageProvider,
        video: VideoProvider,
        voice: VoiceProvider,
    ) -> None:
        self.story = story
        self.image = image
        self.video = video
        self.voice = voice
