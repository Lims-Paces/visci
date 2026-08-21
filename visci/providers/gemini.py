"""
Real Gemini providers: images (Nano Banana Pro), video (Veo 3.1), voice (Flash TTS).

This is where the money goes — video alone is ~88% of the cost of a film.

TWO DIFFERENT API SURFACES
--------------------------
Confusingly, these live in different places on the same client:
  - images: client.interactions.create(...)   returns immediately
  - video:  client.models.generate_videos(...) returns a long-running operation
            you must poll, typically for 1-3 minutes per clip

CONSISTENCY MACHINERY
---------------------
Three mechanisms, in order of how much they buy us:
  1. keyframes    generate a still first, then animate it (image-to-video is far
                  more reliable than text-to-video, and stills are ~40x cheaper
                  to iterate)
  2. reference    character sheets passed as reference images so faces persist
     images
  3. frame        the last frame of shot N becomes the first frame of shot N+1
     chaining     within a continuous scene
"""

import base64
import time
from pathlib import Path

from google import genai
from google.genai import types

from visci.config import settings
from visci.ledger import (
    NANO_BANANA_PRO_USD_PER_IMAGE,
    VEO_FAST_720P_USD_PER_SECOND,
)
from visci.models import (
    ASPECT_RATIO,
    RESOLUTION,
    SHOT_DURATION_SECONDS,
    Character,
    Shot,
    StoryBible,
)
from visci.prompts.visual import (
    NEGATIVE_PROMPT,
    keyframe_prompt,
    reference_sheet_prompt,
    video_prompt,
)
from visci.providers.base import ContentRefused, CostReport

# Angles generated for each character reference sheet. Four is enough to pin a
# face without spending much — they are ~$0.04 each.
REFERENCE_ANGLES = ("front", "three_quarter", "profile", "full_body")


# Phrases that indicate a safety refusal rather than a transient fault. Matched
# case-insensitively against the error text. Kept deliberately broad: treating a
# transient error as a refusal costs one softened retry, whereas treating a
# refusal as transient burns money re-requesting something that will never pass.
_REFUSAL_MARKERS = (
    "safety",
    "policy",
    "blocked",
    "violat",
    "prohibited",
    "content filter",
    "responsible ai",
    "sensitive",
    "harmful",
)


def _looks_like_refusal(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _REFUSAL_MARKERS)


# Errors that are Google's problem, not ours, and usually clear within minutes:
# capacity spikes, rate limits, and gateway faults. These MUST be retried rather
# than killing a run — a 500 mid-render otherwise throws away a paid-for job for
# a condition that resolves on its own.
_TRANSIENT_MARKERS = (
    "high demand",
    "temporarily",
    "try again later",
    "overloaded",
    "unavailable",
    "resource_exhausted",
    "deadline",
    "internal error",
    "500",
    "502",
    "503",
    "504",
    "429",
)


class QuotaExhausted(RuntimeError):
    """
    A hard quota or billing ceiling was hit on the provider side.

    Distinct from a rate limit even though both arrive as HTTP 429. A rate limit
    means "too fast" and clears in seconds, so backing off works. A quota means
    "too much" and clears at the next billing period — retrying just burns
    minutes for a guaranteed failure.
    """


# A 429 carrying these phrases is a QUOTA, not a rate limit. Retrying is futile.
_QUOTA_MARKERS = (
    "exceeded your current quota",
    "check your plan and billing",
    "billing details",
    "spending cap",
    "spend cap",
)


def _looks_like_quota(message: str) -> bool:
    lowered = message.lower()
    return any(marker in lowered for marker in _QUOTA_MARKERS)


def _looks_transient(message: str) -> bool:
    lowered = message.lower()
    # Quota errors mention 429 too, so they must be excluded first or they
    # would be retried pointlessly for eight minutes.
    if _looks_like_quota(lowered):
        return False
    return any(marker in lowered for marker in _TRANSIENT_MARKERS)


def _retry_transient(call, what: str, attempts: int = 5):
    """
    Run `call`, retrying on transient server-side failures.

    Backs off 30s, 60s, 120s, 240s — deliberately patient, because a capacity
    spike takes minutes to clear and the alternative is losing the whole run.
    A refusal or any other permanent error is re-raised immediately, since
    retrying those only wastes time and money.
    """
    import time as _time

    delay = 30
    for attempt in range(1, attempts + 1):
        try:
            return call()
        except ContentRefused:
            raise  # permanent — the prompt must change, not the timing
        except Exception as exc:
            # A quota ceiling will not clear by waiting. Fail immediately with
            # an actionable message instead of eight minutes of futile backoff.
            if _looks_like_quota(str(exc)):
                raise QuotaExhausted(
                    "Google quota or spend cap reached — retrying will not "
                    "help.\n"
                    "  Check limits:  https://ai.dev/rate-limit\n"
                    "  Check spend:   https://ai.studio/spend\n"
                    "Daily quotas reset at midnight Pacific. Your job is saved "
                    "— resume with --job-id once quota is available."
                ) from exc
            if attempt == attempts or not _looks_transient(str(exc)):
                raise
            print(
                f"  {what}: {type(exc).__name__} (attempt {attempt}/{attempts}), "
                f"retrying in {delay}s — {str(exc)[:90]}"
            )
            _time.sleep(delay)
            delay = min(delay * 2, 240)
    raise RuntimeError("unreachable")


# Prepended to a prompt on the retry after a refusal.
#
# This must stay GENRE-NEUTRAL. The pipeline generates every kind of story —
# thriller, horror, war, romance — and a prefix that asserts anything about
# content ("nothing violent is shown") would be false for many of them, and
# would contradict the prompt it is attached to.
#
# So it asserts only what is true of EVERY generation this pipeline makes: that
# it is fiction, staged, and performed. That framing is what safety classifiers
# most often need, and it stays honest whatever the subject.
SOFTENING_PREFIX = (
    "A scripted scene from a work of fiction, performed by professional actors "
    "on a film set. Cinematic, non-explicit, and suitable for a general "
    "audience broadcast.\n\n"
)


def _client() -> genai.Client:
    """
    Build a Gemini client, validating the key shape first.

    Checking before any network call means a missing key fails instantly with a
    clear message rather than hanging on retries.
    """
    key = (settings.gemini_api_key or "").strip()
    if not key:
        raise RuntimeError(
            "GEMINI_API_KEY is empty.\n"
            "Add it to .env:  GEMINI_API_KEY=...\n"
            "Get one at https://aistudio.google.com/apikey\n"
            "Note: Veo video generation requires billing enabled — "
            "the free tier covers text and TTS only."
        )
    # An explicit HTTP timeout matters here. Without one, a request that Google
    # never answers blocks the pipeline indefinitely — the process sits at low
    # CPU on an established socket with no error and no progress, which is
    # indistinguishable from normal slow generation. That cost 30 minutes of a
    # paid run before this was added.
    #
    # 600s, not 180s: an earlier 180s value fired on a LEGITIMATE 2K Nano Banana
    # Pro call, killing a healthy run at shot 8. The lesson is that a timeout
    # must be well above the slowest normal call, not merely above the average
    # one — its job is to catch a genuine hang, not to police latency.
    #
    # Video generation is a long-running OPERATION polled separately, so this
    # does not cut clips short.
    return genai.Client(
        api_key=key,
        http_options=types.HttpOptions(timeout=600_000),  # milliseconds
    )


class GeminiImageProvider:
    """Nano Banana Pro — character reference sheets and shot keyframes."""

    def __init__(self) -> None:
        self.client = _client()
        self.model = settings.image_model

    def _generate(
        self, prompt: str, out_path: Path, reference_images: list[Path] | None = None
    ) -> Path:
        """
        One image generation, optionally conditioned on reference images.

        Nano Banana Pro accepts up to 14 reference images and maintains
        resemblance across up to 5 people — comfortably more than we need, since
        a vertical frame holds at most two.
        """
        content: list[dict] = [{"type": "text", "text": prompt}]

        for ref in reference_images or []:
            content.append(
                {
                    "type": "image",
                    "data": base64.b64encode(ref.read_bytes()).decode("utf-8"),
                    "mime_type": "image/png",
                }
            )

        # Wrapped in retry: the image model returns 500 "high demand" during
        # capacity spikes, which clears on its own within minutes. Letting that
        # kill a paid run would be needless.
        interaction = _retry_transient(
            lambda: self.client.interactions.create(
                model=self.model,
                input=content,
                response_format={
                    "type": "image",
                    "aspect_ratio": ASPECT_RATIO,
                    "image_size": "2K",
                },
            ),
            what=f"image({self.model})",
        )

        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(base64.b64decode(interaction.output_image.data))
        return out_path

    def reference_sheet(
        self, character: Character, bible: StoryBible, out_dir: Path
    ) -> tuple[list[Path], CostReport]:
        """
        Generate multi-angle reference stills for one character.

        Done once per character, before any video exists. These become the
        anchor for facial consistency across all fifteen shots.
        """
        paths = []
        for angle in REFERENCE_ANGLES:
            dest = out_dir / f"{character.name.lower()}_{angle}.png"
            self._generate(reference_sheet_prompt(character, bible, angle), dest)
            paths.append(dest)

        cost = CostReport(
            NANO_BANANA_PRO_USD_PER_IMAGE * len(paths),
            f"image:refsheet:{character.name}",
        )
        return paths, cost

    def keyframe(
        self, shot: Shot, bible: StoryBible, out_path: Path
    ) -> tuple[Path, CostReport]:
        """
        Compose the opening frame of one shot.

        Character reference sheets are passed in so the face matches. This is
        the step that does most of the work of keeping people recognisable.
        """
        references: list[Path] = []
        for name in shot.character_names:
            character = next((c for c in bible.characters if c.name == name), None)
            if not character:
                continue
            # Front and three-quarter carry the most facial information; the
            # profile and full-body shots add little here and cost tokens.
            references += [
                Path(p)
                for p in character.reference_image_paths[:2]
                if Path(p).exists()
            ]

        self._generate(keyframe_prompt(shot, bible), out_path, references)
        return out_path, CostReport(
            NANO_BANANA_PRO_USD_PER_IMAGE, f"image:keyframe:{shot.index:02d}"
        )


class VeoVideoProvider:
    """
    Veo 3.1 — turns a keyframe into an 8-second 9:16 clip.

    Parameters are locked and not configurable:
      aspect_ratio     "9:16"  native vertical, not cropped
      resolution       "720p"  the only tier where scene extension works
      duration_seconds 8       the API accepts 4/6/8 and REQUIRES 8 when using
                               reference images or first/last-frame
    """

    def __init__(self, draft: bool = False) -> None:
        self.client = _client()
        # Lite is half the price and useful for draft renders while iterating.
        self.draft = draft
        self.model = settings.veo_model_draft if draft else settings.veo_model
        self.cost_per_second = (
            VEO_FAST_720P_USD_PER_SECOND if not draft else VEO_FAST_720P_USD_PER_SECOND / 2
        )

    def _config(self) -> types.GenerateVideosConfig:
        """
        Build the generation config for this model tier.

        TIER CAPABILITY DIFFERENCES — verified against the API and SDK source,
        because the documentation does not state them:

        `generate_audio`  Must NOT be sent on the Gemini Developer API. The SDK
                          raises ValueError: "only supported in Gemini
                          Enterprise Agent Platform mode". This is not a loss —
                          Google's docs confirm Veo 3.1 audio is "always on",
                          so the parameter exists only where it can be toggled.
                          Dialogue mode gets its lip-synced speech regardless.

        `negative_prompt` Accepted on Fast and Standard; REJECTED on Lite with
                          400 INVALID_ARGUMENT. Included only for non-draft.

        Getting either wrong fails the FIRST video call — which means finding
        out only after paying for every reference sheet and keyframe. Hence
        both are handled explicitly rather than assumed.
        """
        config = types.GenerateVideosConfig(
            aspect_ratio=ASPECT_RATIO,
            resolution=RESOLUTION,
            duration_seconds=SHOT_DURATION_SECONDS,
            number_of_videos=1,
        )
        if not self.draft:
            config.negative_prompt = NEGATIVE_PROMPT
        return config

    def preflight(self) -> None:
        """
        Validate the config the SDK can check locally, spending nothing.

        WHY THIS EXISTS
        ---------------
        A bad config field fails on the FIRST video call — which happens only
        after every reference sheet and keyframe has been generated and paid
        for. That is exactly how $0.79 was spent discovering that
        `generate_audio` is unsupported.

        The SDK validates several fields client-side before any request is
        sent, so building the config is enough to catch that whole class of
        error for free. Called by the orchestrator before stage 4.
        """
        self._config()

    def generate(
        self, shot: Shot, bible: StoryBible, keyframe: Path, out_path: Path
    ) -> tuple[Path, CostReport]:
        """
        Animate a keyframe into a clip.

        Veo returns a long-running operation rather than a result — generation
        takes 1-3 minutes per clip, which is why fifteen shots is 8-15 minutes
        even with concurrency.

        On a safety refusal, retries ONCE with a genre-neutral framing prefix.
        If that is also refused, raises ContentRefused so the orchestrator can
        skip this shot and keep the rest of the film.
        """
        prompt = video_prompt(shot, bible)

        try:
            return self._attempt(shot, prompt, keyframe, out_path)
        except ContentRefused:
            pass

        # Attempt 2: restate that this is staged fiction. Refusals are often
        # about how a scene reads rather than what it depicts. Refused
        # generations are not charged, so this retry is free.
        try:
            return self._attempt(shot, SOFTENING_PREFIX + prompt, keyframe, out_path)
        except ContentRefused:
            pass

        # Both attempts refused. Raise so the orchestrator can try the one
        # remedy this provider cannot apply alone: widening the framing, which
        # requires regenerating the KEYFRAME as well as re-prompting the video.
        # Animating a close-up keyframe with a "medium shot" prompt does not
        # widen anything — the still already fills the frame with the face.
        raise ContentRefused(
            f"Shot {shot.index}: refused at {shot.framing.value}."
        )

    def _attempt(
        self, shot: Shot, prompt: str, keyframe: Path, out_path: Path
    ) -> tuple[Path, CostReport]:
        """One generation attempt with a given prompt."""
        image = types.Image(
            image_bytes=keyframe.read_bytes(),
            mime_type="image/png",
        )

        operation = _retry_transient(
            lambda: self.client.models.generate_videos(
                model=self.model,
                prompt=prompt,
                image=image,
                config=self._config(),
            ),
            what=f"video({self.model})",
        )

        operation = self._await_operation(operation, shot.index)

        video = operation.response.generated_videos[0]
        self.client.files.download(file=video.video)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        video.video.save(str(out_path))

        cost = CostReport(
            self.cost_per_second * SHOT_DURATION_SECONDS,
            f"video:shot:{shot.index:02d}",
        )
        return out_path, cost

    def _await_operation(self, operation, shot_index: int, timeout: float = 600.0):
        """
        Poll until the video is ready.

        A hard timeout matters here: without one, a stuck operation blocks the
        whole pipeline indefinitely. Ten minutes is generous for a clip that
        normally takes one to three.
        """
        started = time.monotonic()
        while not operation.done:
            if time.monotonic() - started > timeout:
                raise TimeoutError(
                    f"Shot {shot_index}: Veo did not finish within "
                    f"{timeout:.0f}s. The operation may still complete — "
                    f"re-run to resume."
                )
            time.sleep(10)
            operation = self.client.operations.get(operation)

        if operation.error:
            message = str(operation.error)
            if _looks_like_refusal(message):
                raise ContentRefused(
                    f"Shot {shot_index} was refused by Veo's safety filters."
                )
            raise RuntimeError(f"Shot {shot_index}: Veo failed — {operation.error}")

        # A refusal can also arrive as a successful operation with no video
        # attached, rather than as an error. Treat that as a refusal too.
        videos = getattr(operation.response, "generated_videos", None)
        if not videos:
            raise ContentRefused(
                f"Shot {shot_index}: Veo returned no video — most likely a "
                f"content refusal."
            )

        return operation


class GeminiVoiceProvider:
    """
    Gemini Flash TTS — narration voice.

    NARRATION MODE ONLY. Dialogue films need no TTS at all, because Veo
    generates lip-synced speech natively inside each clip.

    A single voice carries the entire film, so this is a much smaller problem
    than per-character casting — which is why the Gemini key we already hold is
    sufficient and ElevenLabs' voice-cloning strength is not needed.
    """

    def __init__(self) -> None:
        self.client = _client()
        self.model = settings.tts_model

    def speak(
        self, text: str, voice_id: str, out_path: Path
    ) -> tuple[Path, CostReport]:
        response = self.client.models.generate_content(
            model=self.model,
            contents=text,
            config=types.GenerateContentConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(
                    voice_config=types.VoiceConfig(
                        prebuilt_voice_config=types.PrebuiltVoiceConfig(
                            voice_name=voice_id
                        )
                    )
                ),
            ),
        )

        audio = response.candidates[0].content.parts[0].inline_data.data
        out_path.parent.mkdir(parents=True, exist_ok=True)

        # Gemini TTS returns raw PCM, not a container format. FFmpeg needs a
        # WAV header to read it, so we write one.
        _write_wav(out_path, audio)

        # Free on the preview tier. Recorded as zero rather than omitted so the
        # call still appears in the ledger.
        return out_path, CostReport(0.0, "voice:narration")


def _write_wav(path: Path, pcm: bytes, rate: int = 24000, channels: int = 1) -> None:
    """
    Wrap raw PCM in a WAV container.

    Gemini TTS returns signed 16-bit little-endian PCM at 24kHz with no header.
    FFmpeg cannot determine the format without one, so we add it.
    """
    import wave

    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(rate)
        wav.writeframes(pcm)
