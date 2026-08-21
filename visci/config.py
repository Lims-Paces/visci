"""
Configuration and spend limits.

Settings are read from environment variables or a .env file, never hardcoded.
Two reasons: API keys must not end up in git, and you want to change model tiers
or spend caps without editing code.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
FIXTURES_DIR = PROJECT_ROOT / "fixtures"


class Settings(BaseSettings):
    """
    All runtime configuration.

    Anything here can be overridden by an environment variable of the same name,
    or a line in a .env file at the project root.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---------------------------------------------------------------- keys --
    # Only TWO keys for the whole pipeline:
    #   - Anthropic  -> Claude, for story/script/shot-list
    #   - Gemini     -> Veo (video), Nano Banana (images), AND Flash TTS (voice)
    #
    # Both are optional because mock mode needs neither. This is deliberate: you
    # can clone the repo and run the entire pipeline with zero credentials.
    anthropic_api_key: str | None = None
    gemini_api_key: str | None = None

    # ---------------------------------------------------------------- mode --
    # THE most important setting during development.
    #
    # When True, every provider returns instant fixture data and costs nothing.
    # When False, real API calls happen and real money is spent.
    #
    # It defaults to True so that the dangerous option is always the one you
    # have to consciously choose. Safe by default.
    mock_mode: bool = True

    # --------------------------------------------------------------- models --
    # Named here rather than inline so a model swap is a one-line change.
    # The -preview suffix on Veo IDs will change at GA; this is where you'd fix
    # that, once.
    claude_model: str = "claude-opus-5"
    veo_model: str = "veo-3.1-fast-generate-preview"
    veo_model_draft: str = "veo-3.1-lite-generate-preview"
    # Nano Banana Pro. Note images use a DIFFERENT API surface to video:
    # client.interactions.create(), not client.models.generate_videos().
    image_model: str = "gemini-3-pro-image"

    # Voice for NARRATION mode only. Dialogue mode needs no TTS at all — Veo
    # generates lip-synced speech natively inside the clip.
    #
    # Why Gemini TTS over ElevenLabs: we already hold this key for video and
    # images, so it is one fewer provider, one fewer bill, and a free tier that
    # covers all of development. Quality benchmarks at or above ElevenLabs for
    # straight narration. ElevenLabs' remaining edge is voice *cloning* — the
    # ability to reproduce a specific real person — which we do not need.
    #
    # Caveats worth remembering: this is a preview model, so the free tier and
    # rate limits can change, and free-tier content may be used by Google for
    # product improvement. Both are survivable because voice sits behind a
    # provider interface (see providers/voice.py) — swapping it is one class.
    #
    # Escape hatch if the free tier disappears or you want zero external
    # dependency: Kokoro-82M runs locally on CPU in ~2-3GB and is Apache
    # licensed, so commercial use is fine. Avoid XTTS v2 despite its popularity
    # — its licence is non-commercial only.
    tts_model: str = "gemini-3.1-flash-tts-preview"

    # A single narrator voice carries the whole film. Which one is chosen per
    # job based on the narration person (1st/2nd/3rd) and tone.
    default_narrator_voice: str = "Charon"

    # ---------------------------------------------------------------- spend --
    # Hard ceilings. The pipeline aborts rather than exceeding these.
    #
    # This exists because an unattended retry bug can burn $100 in an hour. The
    # ceiling is not a nicety — it is the thing standing between a bug and your
    # credit card.
    max_cost_per_job_usd: float = 25.0
    max_cost_per_day_usd: float = 100.0

    # ------------------------------------------------------------ behaviour --
    # How many video generations run at once. Provider rate limits are the real
    # ceiling; going wider does not make the film finish faster, it just gets
    # you throttled.
    max_concurrent_videos: int = 5

    # How many times to retry a single failed shot before giving up on it.
    max_shot_retries: int = 2


settings = Settings()
