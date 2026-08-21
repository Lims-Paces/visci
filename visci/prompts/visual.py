"""
Prompts for image and video generation.

THE CORE TECHNIQUE
------------------
Every visual prompt opens with the SAME verbatim block from the story bible —
character descriptions, palette, film stock, lens language. Never paraphrased,
never summarised, never "improved" between shots.

This matters more than it sounds. The models generate each shot independently
with no memory of the others. If shot 3's prompt says "weathered woman in a navy
sweater" and shot 9's says "older woman in a dark jumper", you get two different
people. Drift in the prompt becomes drift on screen.

So these functions are mostly string assembly, and the discipline is: quote the
bible, don't rewrite it.
"""

from visci.models import Character, Framing, Shot, StoryBible

# How each framing should be phrased to an image model. The enum values are
# snake_case for code; these are the cinematic terms the models understand.
FRAMING_LANGUAGE: dict[Framing, str] = {
    Framing.CLOSE_UP: (
        "tight close-up, face fills the vertical frame, shallow depth of field"
    ),
    Framing.MEDIUM_CLOSE_UP: (
        "medium close-up, head and shoulders, background softly out of focus"
    ),
    Framing.MEDIUM: "medium shot, waist up, subject centred in the vertical frame",
    Framing.OVER_THE_SHOULDER: (
        "over-the-shoulder shot, foreground shoulder soft and out of focus"
    ),
    Framing.LOW_ANGLE_FULL_BODY: (
        "low angle full body shot, camera below eye level looking up, "
        "subject fills the tall frame"
    ),
    Framing.HIGH_ANGLE: "high angle shot looking down at the subject",
    Framing.INSERT_DETAIL: (
        "extreme close-up insert of a detail, macro, no face visible"
    ),
}


def style_block(bible: StoryBible) -> str:
    """
    The visual grammar, identical in every single prompt.

    This is the block that makes fifteen separately-generated clips feel like
    one film. It is quoted verbatim, always.
    """
    return (
        f"VISUAL STYLE: {bible.visual_style}\n"
        f"COLOUR: {bible.color_palette}\n"
        f"CAMERA: {bible.lens_language}\n"
        f"MOOD: {bible.tone}"
    )


def character_block(character: Character) -> str:
    """One character's frozen description, quoted verbatim from the bible."""
    return (
        f"{character.name}: {character.face} "
        f"Build: {character.build} "
        f"Wearing: {character.wardrobe}"
    )


def reference_sheet_prompt(character: Character, bible: StoryBible, angle: str) -> str:
    """
    Prompt for one angle of a character reference sheet.

    These stills are generated ONCE and then fed as reference images into every
    keyframe this character appears in. They are the anchor for facial
    consistency across the whole film, so they are worth getting right — and
    they are cheap to regenerate compared to a video clip.

    Deliberately neutral: plain background, even light, no drama. We want a
    clear record of what this person looks like, not a beautiful photograph.
    Dramatic lighting here would bake shadows into the reference and confuse
    every downstream generation.
    """
    angle_language = {
        "front": "facing camera directly, straight-on portrait",
        "three_quarter": "three-quarter view, head turned 45 degrees",
        "profile": "full profile, head turned 90 degrees to the side",
        "full_body": "full body standing, facing camera, entire figure visible",
    }[angle]

    return (
        f"Character reference photograph. {angle_language}.\n\n"
        f"{character_block(character)}\n\n"
        f"Neutral expression. Plain mid-grey studio background. "
        f"Flat even lighting with no strong shadows. Sharp focus throughout. "
        f"The subject must be clearly and completely visible.\n\n"
        f"This is a reference document, not a dramatic photograph — "
        f"clarity over mood.\n\n"
        f"Style note for consistency: {bible.visual_style}"
    )


def keyframe_prompt(shot: Shot, bible: StoryBible) -> str:
    """
    Prompt for the opening frame of one shot.

    THE HIGHEST-LEVERAGE PROMPT IN THE PIPELINE.

    Generating a still and then animating it converts an unreliable
    text-to-video call into a much more reliable image-to-video one. Stills also
    cost roughly 40x less than clips, so this is where you iterate.
    """
    characters = [c for c in bible.characters if c.name in shot.character_names]
    location = next(
        (loc for loc in bible.locations if loc.name == shot.location_name), None
    )

    parts = [
        f"Vertical 9:16 film still. {FRAMING_LANGUAGE[shot.framing]}.",
        "",
        f"ACTION: {shot.action}",
    ]

    if characters:
        parts += ["", "CHARACTERS IN FRAME:"]
        parts += [character_block(c) for c in characters]

    if location:
        parts += [
            "",
            f"LOCATION: {location.name}. {location.description}",
            f"LIGHTING: {location.lighting}",
        ]

    parts += ["", style_block(bible)]
    parts += [
        "",
        (
            "Composed for a tall vertical frame. Cinematic, photographic, "
            "not illustrated. No text, no watermarks, no letterboxing."
        ),
    ]

    return "\n".join(parts)


# When a shot is refused, we retry at a wider framing before giving up.
#
# WHY: observed refusals cluster on tight close-ups of a character in distress
# or carrying a visible injury. Pulling back to a medium shot puts the face in
# context rather than filling the frame with it, which both renders more
# reliably and is often the better shot — a held close-up on suffering is
# weaker filmmaking than letting the cut do the work.
#
# Maps each framing to the next-wider one. Framings already wide enough have no
# fallback, so a refusal there is genuinely about content, not scale.
WIDER_FRAMING: dict[Framing, Framing] = {
    Framing.CLOSE_UP: Framing.MEDIUM_CLOSE_UP,
    Framing.MEDIUM_CLOSE_UP: Framing.MEDIUM,
    Framing.INSERT_DETAIL: Framing.MEDIUM,
    Framing.OVER_THE_SHOULDER: Framing.MEDIUM,
}


def video_prompt(shot: Shot, bible: StoryBible) -> str:
    """
    Prompt for animating a keyframe into an 8-second clip.

    Different emphasis to the keyframe prompt: the still already establishes
    WHO and WHERE, so this concentrates on MOTION — what moves, how the camera
    behaves, and (in dialogue mode) what is said.

    Keep motion modest. Eight seconds is short, and ambitious camera moves are
    where these models most visibly fall apart.
    """
    parts = [
        f"{FRAMING_LANGUAGE[shot.framing]}.",
        "",
        f"ACTION: {shot.action}",
    ]

    if shot.line and shot.speaker:
        # Dialogue mode: Veo generates lip-synced speech natively inside the
        # clip. Quoting the line explicitly is what triggers that.
        parts += [
            "",
            f'{shot.speaker} says, clearly and in sync: "{shot.line}"',
        ]

    parts += [
        "",
        (
            f"CAMERA: {bible.lens_language} Subtle movement only — "
            f"a slow push, a gentle drift, or a locked-off static frame."
        ),
        "",
        style_block(bible),
        "",
        "Continuous single shot, no cuts. Natural motion.",
    ]

    return "\n".join(parts)


# Applied to every video generation. These are the failure modes that most
# commonly ruin an otherwise good clip.
NEGATIVE_PROMPT = (
    "text, subtitles, captions, watermark, logo, letterboxing, black bars, "
    "split screen, collage, distorted face, extra fingers, extra limbs, "
    "morphing features, jump cut, scene change, camera shake"
)
