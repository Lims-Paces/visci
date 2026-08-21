"""
Script export in the formats the film industry actually uses.

WHY DOCX AND FOUNTAIN
---------------------
Markdown is developer-facing — someone opening the export gets a plain-text
file, not a document. So two formats, each serving a real audience:

  script.docx      Opens in Word, Pages, and Google Docs, and is EDITABLE.
                   That last part is the point: a script you can rewrite is
                   more useful than a finished artifact you can only read.

  script.fountain  Plain-text screenplay markup that Final Draft, Highland,
                   Slugline, and WriterDuet all import. Still readable in a
                   text editor.

The .docx follows standard screenplay typography — 12pt Courier, 1.5" left
margin, dialogue indented — because those conventions are how a script is
recognised as a script, and because they survive being opened in Word.
"""

from io import BytesIO

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from visci.models import JobState, Mode

# Screenplay typography. These measurements are the industry standard, not
# arbitrary: Courier 12pt at these indents is what makes a page of script
# equal roughly one minute of screen time, which is why the conventions exist.
FONT = "Courier"
FONT_BOLD = "Courier-Bold"
SIZE = 12
LEADING = 14.4  # 12pt single-spaced


def _para(doc, text: str, *, bold=False, italic=False, size=SIZE,
          left=0.0, right=0.0, align=None, space_after=12, caps=False):
    """
    One paragraph in screenplay typography.

    Indents are given in inches from the left MARGIN, which is itself set to
    1.5" on the document — the standard screenplay left margin.
    """
    p = doc.add_paragraph()
    if left:
        p.paragraph_format.left_indent = Inches(left)
    if right:
        p.paragraph_format.right_indent = Inches(right)
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    p.paragraph_format.space_before = Pt(0)

    run = p.add_run(text.upper() if caps else text)
    run.font.name = FONT
    run.font.size = Pt(size)
    run.bold = bold
    run.italic = italic
    return p


def script_docx(state: JobState) -> bytes:
    """
    Render the script as an editable Word document.

    Editability is the point. A PDF is a finished artifact you can only read;
    a .docx lets someone rewrite a line of dialogue and hand it back, which is
    what actually happens to scripts.

    Includes a title page, the character and location bible, and the shot list
    written as scenes — so the document is both a script and the production
    reference that produced it.
    """
    doc = Document()

    # Standard screenplay page setup. Courier 12pt at a 1.5" left margin is
    # what makes one page approximate one minute of screen time.
    section = doc.sections[0]
    section.left_margin = Inches(1.5)
    section.right_margin = Inches(1.0)
    section.top_margin = Inches(1.0)
    section.bottom_margin = Inches(1.0)

    normal = doc.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(SIZE)

    bible = state.bible

    # ------------------------------------------------------------ title page --
    for _ in range(10):
        doc.add_paragraph()

    if bible:
        _para(doc, bible.title, bold=True, size=18, caps=True,
              align=WD_ALIGN_PARAGRAPH.CENTER, space_after=24)
        _para(doc, bible.logline, align=WD_ALIGN_PARAGRAPH.CENTER,
              space_after=36)

    runtime = len(state.script.shots) * 8 if state.script else 0
    _para(doc, f"{state.mode.value.title()} · {runtime} seconds · vertical 9:16",
          align=WD_ALIGN_PARAGRAPH.CENTER)
    doc.add_page_break()

    # ---------------------------------------------------------------- bible --
    if bible:
        _para(doc, "CHARACTERS", bold=True, space_after=18)
        for c in bible.characters:
            _para(doc, f"{c.name.upper()} — {c.role}", bold=True, space_after=6)
            _para(doc, c.face, space_after=6)
            _para(doc, f"Build. {c.build}", space_after=6)
            _para(doc, f"Wardrobe. {c.wardrobe}", space_after=6)
            _para(doc, f"Voice. {c.voice_description}", space_after=18)

        _para(doc, "LOCATIONS", bold=True, space_after=18)
        for loc in bible.locations:
            _para(doc, loc.name.upper(), bold=True, space_after=6)
            _para(doc, loc.description, space_after=6)
            _para(doc, f"Lighting. {loc.lighting}", space_after=18)

        _para(doc, "LOOK", bold=True, space_after=18)
        for label, value in (
            ("Style", bible.visual_style),
            ("Palette", bible.color_palette),
            ("Camera", bible.lens_language),
            ("Tone", bible.tone),
        ):
            _para(doc, f"{label}. {value}", space_after=12)

        doc.add_page_break()

    # ----------------------------------------------------------------- shots --
    if state.script:
        _para(doc, "SHOTS", bold=True, space_after=18)
        for shot in state.script.shots:
            slug = (
                f"{shot.index + 1}. {shot.location_name.upper()} — "
                f"{shot.framing.value.replace('_', ' ').upper()}"
            )
            if shot.refused or not shot.video_path:
                slug += "  [NOT GENERATED]"

            _para(doc, slug, bold=True, space_after=6)
            _para(doc, shot.action, space_after=12)

            if shot.line:
                # Character names sit ~2.2" from the margin, dialogue ~1.0" —
                # the indentation that makes a script look like a script.
                speaker = shot.speaker.upper() if shot.speaker else "NARRATOR (V.O.)"
                _para(doc, speaker, left=2.2, space_after=0)
                _para(doc, shot.line, left=1.0, right=1.5, space_after=12)

    buffer = BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


def script_fountain(state: JobState) -> str:
    """
    The script in Fountain — plain-text screenplay markup.

    Imports directly into Final Draft, Highland, Slugline, and WriterDuet, so
    the script can be taken into real screenwriting software and rewritten
    rather than only read.

    Format rules used here:
      Title:/Credit:   title page metadata, key/value at the top
      INT./EXT. …      a scene heading (a "slug line")
      CHARACTER        an all-caps line introduces dialogue
      (parenthetical)  a delivery note
      =                a synopsis line
      #                a section heading, ignored on export to PDF
    """
    bible = state.bible
    out: list[str] = []

    if bible:
        out += [
            f"Title: {bible.title}",
            "Credit: generated by",
            "Author: Visci",
            f"Draft date: {state.prompt_version}",
            "",
            f"= {bible.logline}",
            "",
        ]

    # The bible goes in as sections and synopses: Fountain keeps them in the
    # file for the writer but leaves them out of a printed script.
    if bible:
        out += ["# Characters", ""]
        for c in bible.characters:
            out += [
                f"= {c.name} ({c.role})",
                f"= {c.face}",
                f"= Build: {c.build}",
                f"= Wardrobe: {c.wardrobe}",
                f"= Voice: {c.voice_description}",
                "",
            ]
        out += ["# Locations", ""]
        for loc in bible.locations:
            out += [f"= {loc.name}: {loc.description}",
                    f"= Lighting: {loc.lighting}", ""]
        out += [
            "# Look", "",
            f"= Style: {bible.visual_style}",
            f"= Palette: {bible.color_palette}",
            f"= Camera: {bible.lens_language}",
            f"= Tone: {bible.tone}",
            "",
        ]

    if state.script:
        out += ["# Script", ""]
        for shot in state.script.shots:
            # Fountain infers a scene heading from INT./EXT.; forcing it with a
            # leading dot guarantees it regardless of the location's name.
            out += [
                (
                    f".{shot.index + 1}. {shot.location_name.upper()} — "
                    f"{shot.framing.value.replace('_', ' ').upper()}"
                ),
                "",
                shot.action,
                "",
            ]
            if shot.line:
                speaker = shot.speaker.upper() if shot.speaker else "NARRATOR"
                out += [speaker]
                if not shot.speaker and state.mode is Mode.NARRATION:
                    out += ["(V.O.)"]
                out += [shot.line, ""]

            if shot.refused or not shot.video_path:
                out += ["[[not generated]]", ""]

    return "\n".join(out)
