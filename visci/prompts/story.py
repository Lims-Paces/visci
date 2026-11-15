"""
The prompts we send to Claude.

Kept separate from provider code on purpose. Prompt wording is the thing you
will tune most often, and it should be editable without touching API plumbing.

A note on versioning: `PROMPT_VERSION` is stored on every job. When you improve
a prompt, films generated before and after will differ — and six months from now
you will want to know which version produced a given result. Bump it whenever
you make a meaningful change.
"""

from visci.models import (
    NARRATION_WORDS_PER_SHOT_GUIDE,
    SHOT_DURATION_SECONDS,
    Format,
    Framing,
    Mode,
    NarrationPerson,
    StoryBible,
    Treatment,
)

# Bumped when prompt wording changes meaningfully, so you can tell which films
# came from which version. v2 added the "how to shoot difficult material"
# guidance after Veo refused a close-up of a woman in visible distress.
PROMPT_VERSION = "v2"


# ---------------------------------------------------------------------------
# Shared context.
#
# Every prompt opens with the same explanation of the medium's constraints.
# This is deliberate: the model writes better when it understands WHY the
# limits exist, rather than being handed arbitrary rules. "Vertical frames
# cannot hold three people" produces better shot lists than "max 2 characters".
# ---------------------------------------------------------------------------

def medium_brief(fmt: Format) -> str:
    return f"""\
You are writing for a very specific medium with hard physical constraints. \
Understanding them is essential to writing well for it.

THE FORMAT
- The finished film is exactly {fmt.runtime_seconds} seconds long.
- It is built from exactly {fmt.shot_count} shots, each exactly \
{SHOT_DURATION_SECONDS} seconds. This is fixed by the generation API and \
cannot vary.
- The frame is VERTICAL (9:16), like a phone screen held upright.

WHAT THIS MEANS FOR THE WRITING
- Every shot is a separate generated clip with no memory of the others. \
Continuity comes from description, not from context.
- A vertical frame is tall and narrow. It holds ONE face beautifully, two \
people awkwardly, and three or more not at all. Write for singles and \
over-the-shoulders.
- Eight seconds is short. One action, one beat, one line. Not a scene.
- Fast cutting is the native grammar here, not a compromise. Lean into it.
- Sustained emotional performance across a scene is not achievable. Plot-\
forward stories work; character studies do not.

WHAT MAKES A GOOD STORY HERE
- A single clear want, established immediately.
- Visual, external action rather than internal states.
- A turn or reveal that lands in the final few shots.
- Strong, specific images that survive being described to a machine.

HOW TO SHOOT DIFFICULT MATERIAL
Serious subjects — violence, grief, addiction, fear, cruelty — belong in this \
medium and you should not avoid them. But the video model that renders these \
shots refuses to generate certain images, and a refused shot leaves a hole in \
the finished film. The techniques below are how experienced directors shoot \
this material anyway, and they happen to be what renders reliably.

- Shoot the AFTERMATH or the ANTICIPATION, never the act. The moment before a \
hand is raised, or the stillness afterwards. Both are more frightening than \
the act itself.
- Put physical contact between people in conflict OFF-FRAME. A sound, a \
shadow, a reaction, an object falling. The audience does the work, and does it \
better than any image would.
- Let the CUT carry the emotion rather than a face performing it. A held \
close-up of someone visibly suffering is both weaker filmmaking and the most \
commonly refused shot there is. Cut to hands, an object, an empty doorway, a \
detail that says the same thing.
- Injuries: healing, covered, or glimpsed. Never fresh, never bleeding, never \
the centre of the frame.
- Describe what a CAMERA sees, not what a person feels. "Her hand stops \
halfway to the handle" renders; "she is terrified and traumatised" does not, \
and reads to the filter as a request for distressing content.
- Weapons, restraint, and children in any threatening context will be refused \
outright. Find another image — there is always one.

This is not softening the story. Implication has always been stronger than \
depiction in short film, and in eight seconds you have no room to stage an act \
convincingly anyway. The horror of a violent household is the perfect party \
photograph, not the fist.\
"""


def treatment_prompt(premise: str, mode: Mode, fmt: Format) -> str:
    """Stage 1: premise -> beat sheet, sized in shots."""
    mode_note = (
        "This is a DIALOGUE film. Characters speak on camera."
        if mode is Mode.DIALOGUE
        else "This is a NARRATION film. A single voice narrates over the images."
    )

    # A four-shot short is NOT a compressed three-act film. Asking for three
    # acts in four shots produces incoherence — a rushed setup, no middle, and
    # a punchline. So the two formats get genuinely different instructions.
    if fmt is Format.SHORT:
        structure = f"""\
Expand this into a treatment for a THIRTY-SECOND SHORT.

This is a single charged moment, not a compressed film. You have \
{fmt.shot_count} shots. There is no room for three acts, and attempting them \
produces something that feels rushed and unfinished.

The shape that works:
  1. An image that establishes the situation immediately, with no preamble.
  2. The thing happening.
  3. The consequence, or the reveal.

Break it into beats totalling EXACTLY {fmt.shot_count} shots. Most beats are \
one shot; two at most.

Start in the middle. Do not explain how we got here — the audience does not \
need it, and you do not have the seconds to spend. End on the strongest image, \
not on a resolution.

Set `act` to 1 for all beats — a short has no act structure.

Write a title that is specific and evocative, never generic."""
    else:
        structure = f"""\
Expand this into a THREE-ACT treatment.

Break it into story beats. Each beat gets a number of shots, and those numbers \
must total EXACTLY {fmt.shot_count}.

Think in shots, not seconds — a beat is 1 to 4 shots. A beat that needs more \
than 4 is really two beats.

Give roughly 4 shots to act one, 6 to act two, and 5 to act three, but adjust \
if the story genuinely needs otherwise.

Write a title that is specific and evocative, never generic."""

    return f"""{medium_brief(fmt)}

{mode_note}

THE PREMISE
{premise}

YOUR TASK
{structure}"""


def bible_prompt(treatment: Treatment, mode: Mode, fmt: Format) -> str:
    """Stage 2: treatment -> characters, locations, visual grammar."""
    beats = "\n".join(
        f"  Act {b.act}, {b.shot_count} shot(s): {b.summary}" for b in treatment.beats
    )

    narration_note = ""
    if mode is Mode.NARRATION:
        narration_note = f"""
NARRATION VOICE
Choose the grammatical person for the narrator and set `narration_person`:
  - {NarrationPerson.FIRST.value}: "I walked into the room." Intimate, \
memoir-like, the narrator is in the story.
  - {NarrationPerson.SECOND.value}: "You walk into the room." Rare, striking, \
puts the viewer inside the story. Good for short tense pieces.
  - {NarrationPerson.THIRD.value}: "She walked into the room." Observational, \
fable-like, flexible. The safe choice.
Pick the one that genuinely serves this story.
"""

    return f"""{medium_brief(fmt)}

THE TREATMENT
Title: {treatment.title}
Logline: {treatment.logline}

Beats:
{beats}
{narration_note}
YOUR TASK
Write the story bible. This document is FROZEN after you write it, and every \
single image and video prompt will quote it verbatim. If a description is \
vague here, the character's face will change between shots.

CHARACTER DESCRIPTIONS ARE THE MOST IMPORTANT THING YOU WRITE.

Bad:  "a young woman with dark hair"
Good: "a woman in her late twenties, sharp jawline, deep-set brown eyes, \
matte olive skin, black hair in a blunt shoulder-length bob with a heavy \
fringe, a small silver stud in her left nostril"

The test: could two different artists, working separately from your \
description alone, draw the same person? If not, it is not specific enough.

Same for wardrobe — one outfit, worn for the entire film, described down to \
fabric and wear. And same for locations.

Keep the cast SMALL. One or two characters is ideal for this format. Three is \
the absolute maximum, and only if they never share a frame.

For visual_style, color_palette, and lens_language: these apply to every shot \
and are what make {fmt.shot_count} separate clips feel like one film. Be specific and \
committed. Name film stocks, reference real cinematography."""


def script_prompt(bible: StoryBible, treatment: Treatment, fmt: Format) -> str:
    """Stage 3: bible + treatment -> the 15-shot list."""
    characters = "\n".join(
        f"  {c.name} ({c.role}): {c.face} | Wears: {c.wardrobe}"
        for c in bible.characters
    )
    locations = "\n".join(f"  {loc.name}: {loc.description}" for loc in bible.locations)
    beats = "\n".join(
        f"  Act {b.act}, {b.shot_count} shot(s): {b.summary}" for b in treatment.beats
    )
    framings = "\n".join(f"  - {f.value}" for f in Framing)

    if bible.mode is Mode.DIALOGUE:
        line_note = f"""\
DIALOGUE MODE
Each shot may carry one spoken line, with `speaker` set to the character who \
says it. The line is generated as lip-synced speech inside the clip, so it \
must be short enough to say naturally in under {SHOT_DURATION_SECONDS} \
seconds — roughly {NARRATION_WORDS_PER_SHOT_GUIDE} words at most.

Not every shot needs a line. Silence is powerful, and reaction shots without \
dialogue give the film rhythm."""
    else:
        person = bible.narration_person.value if bible.narration_person else "third"
        line_note = f"""\
NARRATION MODE
Write narration in the {person.upper()} person, consistently, for every line.

Each shot carries one narration line spoken over the image. Leave `speaker` \
empty — a single narrator voices the whole film.

Keep each line to roughly {NARRATION_WORDS_PER_SHOT_GUIDE} words. This is a \
guide for pacing, not a hard rule — but a line much longer than this will \
overrun its shot and desynchronise from the picture.

Narration should COUNTERPOINT the image, not describe it. If we see her open \
the door, do not say "she opens the door". Say what it costs her."""

    return f"""{medium_brief(fmt)}

THE STORY BIBLE (frozen — treat every word as fixed)
Title: {bible.title}
Logline: {bible.logline}
Tone: {bible.tone}
Visual style: {bible.visual_style}
Palette: {bible.color_palette}
Lens language: {bible.lens_language}

Characters:
{characters}

Locations:
{locations}

THE BEATS
{beats}

{line_note}

YOUR TASK
Write the shot list: EXACTLY {fmt.shot_count} shots, following the beats in order \
and giving each beat its allotted number of shots.

For each shot:

`action` — describe ONLY what a camera could see. Physical, external, visual. \
No thoughts, no feelings, no backstory. "Her hand stops halfway to the \
handle" not "she hesitates, remembering".

`framing` — the broad shot size. Choose from exactly these:
{framings}

`camera` — THE ACTUAL CAMERA DIRECTION, and one of the most important things \
you write. You are the cinematographer here: the video model sees only a single \
image and a paragraph, while you know the whole film. Do not write a category \
back to me — describe the shot.

Say the lens, the angle, where the subject sits in the TALL VERTICAL FRAME, \
what is sharp and what falls away. The 9:16 frame is the thing to think hardest \
about: it is a portrait window, generous vertically and cramped horizontally.

Bad:   "close up of her face"
Good:  "85mm, her face in the upper third of the tall frame, the empty hallway \
running away below her, everything past two feet falling to soft bokeh"

Two people is the hard case in a vertical frame, and a flat side-by-side \
two-shot does not fit. Solve it deliberately — stack them in depth with one \
soft in the foreground, shoot over a shoulder, use a mirror, or put one at the \
top of frame and one at the bottom. Say which you are doing.

Use the vertical space. A figure small at the bottom with a ceiling above them \
reads very differently from a face filling the frame, and only the tall format \
gives you that.

`character_names` — at most TWO, and prefer one. A vertical frame cannot hold \
more.

`location_name` — must match a location from the bible exactly.

VARY THE FRAMING. A run of identical framings is visually dead. Cut between \
scales — a close-up lands harder after a wide, an insert of a detail gives the \
eye somewhere to rest.

MATCH FRAMING TO CONTENT — THIS IS A HARD RULE
Tight framings and sensitive content do not mix. A tight close-up fills the \
frame with one thing, so whatever is in it becomes the entire image — and a \
close-up whose subject is suffering or injury is the single most commonly \
refused shot there is.

So: if a shot involves an injury, a mark on the body, visible fear or \
distress, the aftermath of violence, or a character concealing harm, then \
DO NOT use `close_up` or `medium_close_up` for it. Use `medium`, \
`low_angle_full_body`, `high_angle`, or `over_the_shoulder` instead.

Save your close-ups for the safe beats — a smile at a party, a hand on a door \
handle, someone listening. There you get their full force, and they render \
every time.

This is not a limitation, it is how the material actually wants to be shot. \
A woman covering a bruise, seen in a medium shot with the room around her, is \
more devastating than the same moment shot tight, because you see the ordinary \
world she has to keep performing in. The wide frame is the story.

BEFORE YOU FINALISE, RE-READ EVERY SHOT AND ASK TWO QUESTIONS:
1. Is this shot's content sensitive? If so, is its framing `medium` or wider? \
If not, widen it now.
2. Would a cautious content filter refuse this image? The usual offenders are \
physical contact during conflict, a fresh or central injury, or a described \
emotional state rather than a described action. If so, rewrite it as an \
aftermath, an off-frame sound, a detail insert, or a reaction — the beat \
survives, and the shot actually renders.

Remember each shot is generated independently. Write each `action` so it makes \
sense in isolation, without relying on the previous shot for context."""
