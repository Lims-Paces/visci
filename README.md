# Visci

Generate a 2-minute vertical short film from a one-line premise.

**Status:** week 1 — the pipeline works end to end in mock mode. Real providers
land in week 2.

---

## The web app

Two servers, both from the project root:

```bash
# terminal 1 — API
source .venv/bin/activate && uvicorn visci.api:app --port 8000 --reload

# terminal 2 — frontend
cd web && npm run dev
```

Open the URL Next.js prints (usually http://localhost:3000). Type an idea, pick
dialogue or narration, and watch the shot grid fill in as the film generates.

Real generation is off by default — the same safe-by-default rule the CLI
follows. The estimated cost is shown before you commit to it.

---

## Quick start

```bash
uv venv && source .venv/bin/activate
uv pip install -e ".[dev]"

visci generate "a lighthouse keeper receives a transmission from a ship that sank forty years ago"
```

That produces a real, playable 720×1280 MP4 in `output/`. It costs nothing and
takes about a second, because it runs against mock providers.

```bash
visci generate "..." --mode narration   # voice-over instead of on-screen dialogue
visci config                            # show settings and whether keys are set
pytest                                  # 13 tests, ~4 seconds
```

## Spending real money

Four cost tiers, so you never spend more than the task needs:

```bash
visci check                             # verify keys work        ~$0.0001
visci generate "..."                    # mock everything              $0
visci generate "..." --story-real       # real script, mock video  ~$0.60
visci generate "..." --real --draft     # real film, Veo Lite         ~$9
visci generate "..." --real             # real film, Veo Fast        ~$17
```

`--story-real` is the one to live in while tuning prompts: you read what Claude
actually writes for 60 cents, without touching the 88% of cost that is video.

`--real` asks for confirmation before spending, and defaults to no.

---

## Locked v1 constraints

These are deliberate, and several are dictated by the Veo API rather than chosen:

| | Value | Why |
|---|---|---|
| Runtime | 120s | Cost and consistency both degrade with length |
| Shots | 15 × 8s | Veo accepts only 4/6/8s, and **requires 8s** with reference images |
| Aspect | 9:16 | Vertical framing prevents the crowded shots models handle worst |
| Resolution | 720p | The **only** tier where Veo scene extension works |

Runtime is the one constraint to treat as product-defining. Aspect and
resolution lift easily later; runtime multiplies cost, wall-clock, and drift.

---

## How it works

```
premise
   ↓  Claude          1. treatment      three-act beats, sized in shots
   ↓  Claude          2. story bible    characters, locations, visual grammar  ← frozen
   ↓  Claude          3. shot list      15 shots, framing constrained by enum
   ↓  Nano Banana     4. reference sheets   multi-angle stills per character
   ↓  Nano Banana     5. keyframes      first frame of each shot, 9:16
   ↓  Veo 3.1 Fast    6. video          8s clip per keyframe
   ↓  Gemini TTS      7. narration      narration mode only
   ↓  FFmpeg          8. assembly       concat + audio mix
finished film
```

**The story bible is the crown jewel.** Generated once, then frozen and quoted
verbatim into every later prompt. If character descriptions drift between
prompts, they drift on screen.

**Keyframes are the highest-leverage step.** Generating a still and then
animating it converts an unreliable text-to-video call into a much more reliable
image-to-video one — and stills cost ~40× less to iterate.

---

## Architecture notes

### Mock providers are why this is affordable to build

Every external service sits behind an interface in `providers/base.py`, with two
implementations: `real.py` (costs money) and `mock.py` (instant, free,
deterministic).

A full pipeline run in mock mode takes ~1 second and costs $0. Against real APIs
it is ~12 minutes and ~$17. Over an 8-week build that is the difference between
roughly $200 and $1,300 in spend — and between iterating in seconds versus
quarter-hours.

The mock fixtures are **real media at Veo's exact output dimensions**
(720×1280, 8s, H.264), so assembly code tested against them behaves identically
when the input becomes real.

### Estimate to plan, measure to verify

Word-count pacing (~19 words per 8s shot) is a *drafting guide* handed to Claude,
never a prediction. Real spoken duration depends on the voice, punctuation, and
pauses. So after synthesis we measure with `ffprobe` and reconcile.

This matters because video shots are locked at exactly 8 seconds but audio is
not. A line running 9.4s overruns its shot, and small drift compounds across 15
shots into visible desync.

### Export gives you the whole project

"Export project" bundles the film, every individual clip, the keyframes, the
character reference sheets, and the script in two formats:

- `script.docx` — editable in Word, Pages, or Google Docs, in standard
  screenplay typography (Courier 12pt, 1.5" left margin, dialogue indented)
- `script.fountain` — imports into Final Draft, Highland, Slugline, WriterDuet

The MP4 is the deliverable, but it is not the whole of what was paid for. The
clips are what someone re-cuts in a real editor; the reference sheets are what
keeps a sequel consistent with this film.

### Concurrency is adaptive, not fixed

Shots generate several at a time — 30+ minutes sequentially becomes under 10.
But naive parallelism makes rate limits *worse*: N workers are N× more likely to
trip one, and they then back off together and retry together, producing bursts
of failure instead of throughput.

So the controller starts at 3 and adapts. A rate limit halves it; sustained
success climbs it back. Retries are jittered so workers don't retry in lockstep.

Two 429s are handled very differently, because they mean opposite things:

| | Meaning | Response |
|---|---|---|
| **Rate limit** | too fast | halve concurrency, jittered retry |
| **Quota** | too much | abort the entire run immediately |

Retrying a quota error is futile — it clears at the next billing period, not in
30 seconds. Without that distinction, every worker independently burns its own
backoff discovering the same wall.

The ledger also gets a veto before each shot dispatches: concurrency means a
runaway bug spends N× faster, so the spend ceiling must be able to stop work
rather than only report on it afterwards.

### Spend ceilings halt, they do not warn

`ledger.py` enforces a per-job and per-day cap. Every provider call passes
through it. The daily total persists to disk so it survives restarts — a
per-process counter would reset exactly when you least want the safety net gone.

This exists because an unattended retry bug is the most likely way this project
costs real money unexpectedly.

### State is saved after every stage

`output/<job-id>/state.json` holds the full job. If shot 12 of 15 fails, the
treatment, bible, script, and first 11 clips survive. Re-running with
`--job-id` resumes rather than restarting.

```bash
visci generate "" --job-id 20260821-110235-d819fe
```

In real mode that turns a $17 failure into a $5 one.

---

## Providers

Two API keys for the whole pipeline:

| Service | Used for | Cost per film |
|---|---|---|
| **Anthropic** — Claude Opus 5 | treatment, bible, shot list | ~$0.60 |
| **Gemini** — Veo 3.1 Fast | video, 15 × 8s clips | ~$15.00 |
| **Gemini** — Nano Banana Pro | reference sheets, keyframes | ~$1.25 |
| **Gemini** — Flash TTS | narration voice | free tier |

Gemini TTS replaced ElevenLabs because we already hold that key for video and
images — one fewer provider and bill, with quality that benchmarks at or above
ElevenLabs for straight narration. ElevenLabs' remaining edge is voice *cloning*,
which we don't need: one narrator voice carries the whole film.

If the TTS free tier disappears, **Kokoro-82M** runs locally on CPU and is
Apache-licensed. Avoid XTTS v2 — non-commercial licence only.

---

## Project layout

```
visci/
  models.py          data shapes + locked constraints. Start here.
  config.py          settings, model ids, spend caps
  ledger.py          spend tracking and hard limits
  cli.py             the week-1 user interface
  providers/
    base.py          interfaces — the key architectural file
    mock.py          instant, free, deterministic
    claude.py        real story generation
    gemini.py        real images, video, and voice
  prompts/
    story.py         what we ask Claude for
    visual.py        what we ask the image and video models for
  pipeline/
    run.py           orchestrator, saves state after every stage
    assemble.py      FFmpeg concat + narration mix
```

---

## How consistency is enforced

Three mechanisms, in order of how much each buys you:

1. **Keyframes before video.** Generate a still, then animate it. Image-to-video
   is far more reliable than text-to-video, and stills cost ~40× less to iterate.
2. **Reference sheets.** Four angles per character, generated once, passed as
   reference images into every keyframe that character appears in.
3. **Verbatim prompt blocks.** Every visual prompt quotes the frozen bible word
   for word. Paraphrasing a description between shots is how faces drift — so
   `prompts/visual.py` assembles strings rather than rewriting them.

There is a test asserting that character descriptions appear verbatim in
generated prompts, because this is easy to break accidentally.

---

## Next

1. Vision-model verification of keyframes against reference sheets, with retry
2. Last-frame → first-frame chaining within continuous scenes
3. Concurrency — currently shots generate one at a time
4. Frontend: premise input + live progress view

Mock remains the default and `--real` asks before spending. Safe by default is
deliberate: the dangerous option should always be the one you consciously choose.
