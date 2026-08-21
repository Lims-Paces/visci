"""
Stage 9: assembly. Fifteen clips in, one film out.

This is pure FFmpeg — no AI, no cost, no network. It is also the stage most
likely to have fiddly bugs, which is precisely why mock mode exists: you can run
this a hundred times in a minute for free until it is right.

WHY CONCAT IS CHEAP HERE
------------------------
Every clip comes from Veo at identical dimensions (720x1280), identical codec
(H.264), and identical frame rate. That means FFmpeg's `concat` demuxer can join
them by copying the streams rather than re-encoding — near-instant, and no
generation loss. If the clips differed in any of those properties we would have
to re-encode, which is slow and degrades quality.

This is a real benefit of the locked 9:16 / 720p / 8-second constraints: it
makes assembly trivial.
"""

import json
import subprocess
from pathlib import Path

from visci.models import SHOT_DURATION_SECONDS, Mode, Script


class AssemblyError(RuntimeError):
    """FFmpeg failed. Carries the stderr so the cause is visible."""


def probe_duration(path: Path) -> float:
    """
    MEASURE the real duration of a media file.

    Note the principle: we never estimate durations from word counts or assume a
    clip is 8 seconds because we asked for 8 seconds. We ask the file.

    This is what catches narration overrunning its shot, and what catches a
    truncated download from Veo.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,  # we inspect returncode ourselves to attach stderr
    )
    if result.returncode != 0:
        raise AssemblyError(f"ffprobe failed on {path}:\n{result.stderr}")
    return float(json.loads(result.stdout)["format"]["duration"])


def verify_clips(script: Script) -> list[str]:
    """
    Check every clip exists and is roughly the right length before assembling.

    Runs BEFORE the expensive concat so a bad clip is caught early with a clear
    message, rather than producing a subtly wrong film you only notice on
    playback.

    Returns a list of problems; empty means all good.
    """
    problems: list[str] = []
    for shot in script.shots:
        if shot.refused:
            # Deliberately excluded, not missing. The film is shorter but still
            # watchable, which beats failing outright after paying for the other
            # fourteen clips.
            continue
        if not shot.video_path:
            problems.append(f"Shot {shot.index}: no video generated")
            continue
        path = Path(shot.video_path)
        if not path.exists():
            problems.append(f"Shot {shot.index}: file missing at {path}")
            continue
        duration = probe_duration(path)
        # Allow a little slack — encoders round frame counts.
        if abs(duration - SHOT_DURATION_SECONDS) > 0.5:
            problems.append(
                f"Shot {shot.index}: expected ~{SHOT_DURATION_SECONDS}s, "
                f"measured {duration:.2f}s"
            )
    return problems


def concat_clips(script: Script, out_path: Path, work_dir: Path) -> Path:
    """
    Join all clips into one file using FFmpeg's concat demuxer.

    The demuxer needs a text file listing inputs, which is why we write one.
    `-c copy` means "copy streams, do not re-encode" — this is what makes it
    fast and lossless.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    list_file = work_dir / "concat_list.txt"
    lines = []
    for shot in script.shots:
        if shot.refused or not shot.video_path:
            continue
        # Absolute paths, single quotes escaped — FFmpeg's list format is picky.
        p = Path(shot.video_path).resolve()
        lines.append(f"file '{str(p).replace(chr(39), chr(39) * 3)}'")

    if not lines:
        raise AssemblyError(
            "No clips available to assemble — every shot was refused or failed."
        )
    list_file.write_text("\n".join(lines))

    result = subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        check=False,  # we inspect returncode ourselves to attach stderr
    )
    if result.returncode != 0:
        raise AssemblyError(f"concat failed:\n{result.stderr}")
    return out_path


def mix_narration(
    video_path: Path, script: Script, out_path: Path, work_dir: Path
) -> Path:
    """
    Lay narration audio over the picture. NARRATION MODE ONLY.

    In dialogue mode this is skipped entirely — Veo already generated
    lip-synced speech inside each clip, so there is nothing to add.

    Each narration line is placed at the start of its own shot, which is at
    `index * 8` seconds into the film. We use `adelay` to offset each one, then
    `amix` to combine them with the original audio.
    """
    narration_shots = [s for s in script.shots if s.audio_path and not s.refused]
    if not narration_shots:
        return video_path

    work_dir.mkdir(parents=True, exist_ok=True)

    # Build the FFmpeg filter graph. Inputs: [0] is the video, [1..n] are the
    # narration files.
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(video_path)]
    for shot in narration_shots:
        cmd += ["-i", str(shot.audio_path)]

    filters = []
    labels = []
    # Offsets must be based on POSITION IN THE ASSEMBLED FILM, not the
    # original shot index. If shot 3 was refused, shot 4's audio belongs at
    # 24s in the finished cut, not 32s — using the raw index would desync
    # every line after the gap.
    position = {
        s.index: i
        for i, s in enumerate(sh for sh in script.shots if not sh.refused and sh.video_path)
    }

    for n, shot in enumerate(narration_shots, start=1):
        offset_ms = position.get(shot.index, shot.index) * SHOT_DURATION_SECONDS * 1000
        # adelay needs a value per channel; `all=1` applies it to every channel.
        filters.append(f"[{n}:a]adelay={offset_ms}:all=1[n{n}]")
        labels.append(f"[n{n}]")

    # Mix the clip's own audio bed with every delayed narration line.
    # `dropout_transition=0` stops amix ducking the volume as inputs end.
    filters.append(
        f"[0:a]{''.join(labels)}amix=inputs={len(labels) + 1}"
        f":duration=first:dropout_transition=0[mixed]"
    )

    cmd += [
        "-filter_complex", ";".join(filters),
        "-map", "0:v",
        "-map", "[mixed]",
        "-c:v", "copy",      # picture untouched
        "-c:a", "aac",       # audio must be re-encoded after mixing
        str(out_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise AssemblyError(f"narration mix failed:\n{result.stderr}")
    return out_path


def assemble(script: Script, out_path: Path, work_dir: Path) -> Path:
    """
    Full assembly: verify, concat, and mix narration if needed.

    Returns the path to the finished film.
    """
    problems = verify_clips(script)
    if problems:
        raise AssemblyError(
            "Cannot assemble, clips are not ready:\n  " + "\n  ".join(problems)
        )

    joined = work_dir / "joined.mp4"
    concat_clips(script, joined, work_dir)

    if script.bible.mode is Mode.NARRATION:
        return mix_narration(joined, script, out_path, work_dir)

    # Dialogue mode: the concatenated file IS the finished film.
    joined.replace(out_path)
    return out_path
