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

from visci.models import Character, Framing, Shot, StoryBible, is_sensitive

# Phrasing used ONLY where a shot's content risks a safety refusal.
#
# Everywhere else we send Claude's own camera direction verbatim — it knows the
# whole film, the vertical frame, and what this moment needs, none of which a
# lookup table can encode.
#
# But on sensitive content the frame must stay wide enough that the difficult
# element sits in context rather than filling the image, because a tight frame
# on an injury is what gets refused. That is a safety constraint, not a
# creative one, so it overrides whatever Claude wrote.
FRAMING_STRICT: dict[Framing, str] = {
    Framing.CLOSE_UP: (
        "medium close-up, head and shoulders, the room visible around them"
    ),
    Framing.MEDIUM_CLOSE_UP: (
        "medium close-up, head and shoulders, background softly out of focus"
    ),
    Framing.MEDIUM: (
        "medium shot, waist up, the subject seen in the context of the room "
        "around them rather than filling the frame"
    ),
    Framing.OVER_THE_SHOULDER: (
        "over-the-shoulder shot, foreground shoulder soft and out of focus"
    ),
    Framing.LOW_ANGLE_FULL_BODY: (
        "full body shot from slightly below, the whole figure in the frame"
    ),
    Framing.HIGH_ANGLE: "shot from above, the subject small in the frame",
    Framing.INSERT_DETAIL: (
        "insert of a detail, no face in frame, the object seen in its setting"
    ),
}


def camera_direction(shot: Shot) -> str:
    """
    How this shot is covered.

    Claude's own direction, except on sensitive shots — where a prescribed
    wider frame is what keeps the generation from being refused, and so
    overrides the creative choice.
    """
    if is_sensitive(shot):
        return FRAMING_STRICT[shot.framing]
    return shot.camera


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

    # Sensitive content keeps a prescribed frame — a tight shot on an injury or
    # visible distress is what gets refused. Everything else gets a suggestion,
    # leaving the exact composition to the model.
    parts = [
        f"Vertical 9:16 film still. {camera_direction(shot)}",
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
            "Cinematic, photographic, not illustrated. Compose exactly as "
            "the camera direction above describes. No text, no watermarks, "
            "no letterboxing."
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


# Appended to every video prompt.
#
# WHY THIS EXISTS
# ---------------
# Generated movement reads as unnatural in a specific, diagnosable way: bodies
# move all at once, at a constant speed, with no sense of weight. Real human
# motion has three properties these models drop unless told otherwise:
#
#   ANTICIPATION  a body shifts weight BEFORE it moves
#   SEQUENCING    parts move in order — eyes lead, then head, then shoulders
#   SETTLING      movement decelerates into stillness, it does not just cease
#
# Naming them explicitly helps. "Natural motion" alone does not, because it
# does not tell the model what natural motion is made of.
MOTION_DIRECTION = (
    "MOTION: Move like a real body. Shift weight before moving. Let one part "
    "lead and the rest follow — eyes before head, head before shoulders. Ease "
    "into stillness rather than stopping dead. Keep small involuntary life "
    "going throughout: breath, blinks, tiny adjustments of balance. One clear "
    "action across the whole shot at an unhurried human tempo, not several "
    "actions rushed together."
)


def video_prompt(shot: Shot, bible: StoryBible) -> str:
    """
    Prompt for animating a keyframe into an 8-second clip.

    DELIBERATELY SHORT
    ------------------
    The keyframe is passed alongside this prompt, so the model can already SEE
    the location, wardrobe, lighting, and composition. Re-describing them here
    spends its attention on things that are not moving.

    An earlier version emitted ~500 words, of which about 60 described the
    actual movement — the rest was film-stock references and colour theory
    duplicated from the style block. It also emitted CAMERA twice with
    contradictory instructions: "subtle movement only" alongside a description
    of handheld drift, dolly-ins, and push-ins.

    So this now sends framing, what moves, the line, and how bodies should
    behave. Nothing the still already answers.
    """
    parts = [
        camera_direction(shot),
        "",
        f"WHAT HAPPENS: {shot.action}",
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
        MOTION_DIRECTION,
        "",
        # Camera behaviour ONLY — no lens or grade detail, which the keyframe
        # already carries. Ambitious camera moves are where these models most
        # visibly fall apart, so we ask for very little.
        (
            "CAMERA: Hold the frame nearly still — locked off, or at most a "
            "very slow push or gentle handheld drift. No whip pans, no sudden "
            "reframes, no zooms."
        ),
        "",
        f"Tone: {bible.tone}",
        "",
        "Continuous single take. No cuts, no scene change.",
    ]

    return "\n".join(parts)


# Applied to every video generation. These are the failure modes that most
# commonly ruin an otherwise good clip.
NEGATIVE_PROMPT = (
    "text, subtitles, captions, watermark, logo, letterboxing, black bars, "
    "split screen, collage, distorted face, extra fingers, extra limbs, "
    "morphing features, jump cut, scene change, camera shake"
)
