#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
lyricvideo.py – turns a still image, a song and your existing lyrics into a lyric video.

Subcommands:
  align    audio + lyrics  ->  editable SRT (forced alignment via ElevenLabs or locally)
  preview  quick look at the subtitle style (PNG still or short clip)
  render   image + audio + SRT  ->  MP4 with burned-in subtitles
  all      align, pause for editing, preview/style switching, render
  web      start the browser UI
  projects list all projects
  styles   list the available style presets

Example:
  python lyricvideo.py all --audio input/song.mp3 --image input/cover.png --text input/song.txt --style grimdark
"""
from __future__ import annotations

import sys

if sys.version_info < (3, 10):
    sys.stderr.write(
        "ERROR: Python 3.10 or newer is required (found: %s).\n"
        "Install it e.g. with: winget install Python.Python.3.12\n" % sys.version.split()[0]
    )
    sys.exit(1)

import argparse
import copy
import hashlib
import importlib.util
import json
import mimetypes
import os
import re
import shutil
import statistics
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV_PY = ROOT / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
OUTPUT_DIR = ROOT / "output"
FONTS_DIR = ROOT / "fonts"
MODELS_DIR = ROOT / "models"
CONFIG_FILE = ROOT / "config.json"
STYLES_FILE = ROOT / "styles.json"
ENV_FILE = ROOT / ".env"

# From the ElevenLabs API reference (POST /v1/forced-alignment): fields "file" and "text",
# file smaller than 1 GB, text up to 675,000 characters, no language parameter.
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/forced-alignment"
ELEVENLABS_MAX_BYTES = 1024 ** 3
ELEVENLABS_MAX_CHARS = 675_000

ASSUME_YES = False  # set by --yes


# --------------------------------------------------------------------------- #
# General helpers
# --------------------------------------------------------------------------- #

class LyricError(Exception):
    """Error with a readable message; aborts the current command."""


class AlignmentMismatch(LyricError):
    """The words of the API response do not match the lyrics."""


def info(msg: str = "") -> None:
    print(msg, flush=True)


def warn(msg: str) -> None:
    print(f"WARNING: {msg}", flush=True)


def rel(p: Path | str) -> str:
    p = Path(p)
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


def fmt_num(x: float, digits: int = 2) -> str:
    """Number with a fixed number of decimals."""
    return f"{x:.{digits}f}"


def ask_input(prompt: str, default: str = "") -> str:
    try:
        ans = input(prompt)
    except EOFError:
        print()
        return default
    return ans.strip() or default


def ask_yes_no(question: str, default: bool = False) -> bool:
    suffix = " [Y/n] " if default else " [y/N] "
    while True:
        ans = ask_input(question + suffix).lower()
        if not ans:
            return default
        if ans in ("y", "yes"):
            return True
        if ans in ("n", "no"):
            return False
        info("Please enter y or n.")


def confirm_overwrite(paths: list[Path], hint: str = "") -> bool:
    """True if writing is allowed. Asks when files already exist."""
    existing = [p for p in paths if p.exists()]
    if not existing or ASSUME_YES:
        return True
    info("The following file(s) already exist:")
    for p in existing:
        info(f"  {rel(p)}")
    if hint:
        info(hint)
    return ask_yes_no("Overwrite?", False)


def read_text_file(path: Path) -> str:
    raw = path.read_bytes()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        warn(f"{rel(path)} is not UTF-8 encoded, reading it as Windows-1252. "
             "Please check that special characters look right (saving as UTF-8 is best).")
        return raw.decode("cp1252", errors="replace")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def require_file(value: str | None, what: str) -> Path:
    if not value:
        raise LyricError(f"{what} is missing.")
    p = Path(value)
    if not p.is_file():
        raise LyricError(f"{what} not found: {value}")
    return p


def parse_time_arg(value: str) -> float:
    """'30', '30.5', '1:23.5' or '0:01:23' -> seconds."""
    s = str(value).strip().replace(",", ".")
    try:
        parts = [float(x) for x in s.split(":")]
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid time: {value!r} (e.g. 30, 30.5 or 1:23)")
    t = 0.0
    for part in parts:
        t = t * 60 + part
    if t < 0:
        raise argparse.ArgumentTypeError("time must not be negative")
    return t


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #

DEFAULT_CONFIG = {
    "width": 1920,
    "height": 1080,
    "fps": 30,
    "default_style": "standard",
    "default_language": "en",
    "default_backend": "elevenlabs",
    "video": {"crf": 18, "preset": "medium", "audio_bitrate": "192k"},
    "preview": {"lowres_factor": 0.5, "crf": 28, "open_after": True},
    "quality": {"min_line_seconds": 0.3, "max_line_seconds": 12.0, "loss_factor": 2.0, "max_first_word_seconds": 3.0,
                "max_gap_seconds": 20.0},
    "alignment": {"line_hold_ms": 0, "api_line_separator": "\n"},
    "local": {"whisper_model": "medium"},
    "web": {"port": 8765},
}


def deep_merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_json_file(path: Path, what: str) -> dict:
    try:
        data = json.loads(read_text_file(path))
    except json.JSONDecodeError as e:
        raise LyricError(f"{what} ({rel(path)}) is not valid JSON: {e.msg} in line {e.lineno}, column {e.colno}.")
    if not isinstance(data, dict):
        raise LyricError(f"{what} ({rel(path)}) must be a JSON object.")
    return data


def load_config() -> dict:
    cfg = DEFAULT_CONFIG
    if CONFIG_FILE.exists():
        user = {k: v for k, v in load_json_file(CONFIG_FILE, "config.json").items() if not k.startswith("_")}
        cfg = deep_merge(DEFAULT_CONFIG, user)
    for key in ("width", "height", "fps"):
        if not isinstance(cfg[key], int) or cfg[key] <= 0:
            raise LyricError(f"config.json: '{key}' must be a positive integer.")
    if cfg["width"] % 2 or cfg["height"] % 2:
        raise LyricError("config.json: width and height must be even numbers (required for yuv420p).")
    return cfg


def load_api_key() -> str:
    key = ""
    if ENV_FILE.exists():
        for line in read_text_file(ENV_FILE).splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            if k.startswith("export "):
                k = k[7:].strip()
            if k == "ELEVENLABS_API_KEY":
                key = v.strip().strip('"').strip("'").strip()
    if not key:
        key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise LyricError(
            "No ElevenLabs API key found.\n"
            "  Copy .env.example to .env and put your key after ELEVENLABS_API_KEY=\n"
            "  (Alternative: --backend local for offline alignment.)"
        )
    return key


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #

_FFMPEG: dict = {}


def check_ffmpeg() -> None:
    if _FFMPEG:
        return
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        raise LyricError(
            "ffmpeg/ffprobe was not found in PATH.\n"
            "  Install it (Windows):  winget install Gyan.FFmpeg\n"
            "  Then reopen the terminal and run the command again."
        )
    try:
        out = subprocess.run([ffmpeg, "-hide_banner", "-filters"], capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30).stdout
    except (OSError, subprocess.SubprocessError) as e:
        raise LyricError(f"ffmpeg could not be started: {e}")
    if not re.search(r"^\s*\S*\s+ass\s", out, re.M):
        raise LyricError(
            "Your ffmpeg does not support the 'ass' filter (libass).\n"
            "  Please install a full build, e.g.:  winget install Gyan.FFmpeg"
        )
    _FFMPEG["ffmpeg"] = ffmpeg
    _FFMPEG["ffprobe"] = ffprobe


def media_duration(path: Path) -> float | None:
    check_ffmpeg()
    r = subprocess.run(
        [_FFMPEG["ffprobe"], "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise LyricError(f"ffmpeg cannot read {rel(path)}:\n  {r.stderr.strip()[:500]}")
    try:
        return float(r.stdout.strip())
    except ValueError:
        return None


def run_ffmpeg(args: list[str], cwd: Path, progress=None) -> None:
    """Runs ffmpeg. With progress(seconds) the progress is reported continuously."""
    if progress is None:
        cmd = [_FFMPEG["ffmpeg"], "-hide_banner", "-loglevel", "error", *args]
        returncode = subprocess.run(cmd, cwd=str(cwd)).returncode
    else:
        cmd = [_FFMPEG["ffmpeg"], "-hide_banner", "-loglevel", "error", "-nostats", "-progress", "pipe:1",
               *[a for a in args if a != "-stats"]]
        proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace")
        for line in proc.stdout:
            if line.startswith("out_time_us="):
                try:
                    progress(int(line.split("=", 1)[1]) / 1_000_000)
                except ValueError:
                    pass
        returncode = proc.wait()
    if returncode != 0:
        code = returncode - 2 ** 32 if returncode > 2 ** 31 else returncode
        raise LyricError(f"ffmpeg stopped with error code {code} (see messages above).")


SAFE_FILTER_NAME = re.compile(r"^[A-Za-z0-9_.\-]+$")


def ass_filter(ass_name: str, ass_dir: Path) -> str:
    """ass filter with a relative file name (ffmpeg runs in the folder of the ASS file)."""
    fontsdir = os.path.relpath(FONTS_DIR, ass_dir).replace("\\", "/")
    if not re.match(r"^[A-Za-z0-9_.\-/]+$", fontsdir):
        fontsdir = "'" + FONTS_DIR.as_posix() + "'"
    return f"ass=filename={ass_name}:fontsdir={fontsdir}"


def base_video_filter(w: int, h: int, dim_percent: float) -> str:
    """Fit the image (no distortion), pad with black, optionally darken – computed once and then
    repeated with the loop filter (much faster than decoding the image again for every frame)."""
    f = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos,"
         f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1")
    if dim_percent > 0:
        f += f",drawbox=x=0:y=0:w=iw:h=ih:color=black@{dim_percent / 100:.3f}:t=fill"
    return f + ",format=yuv420p,loop=loop=-1:size=1:start=0"


def image_input_args(image: Path, fps: int) -> list[str]:
    return ["-f", "image2", "-pattern_type", "none", "-framerate", str(fps), "-i", str(image.resolve())]


# --------------------------------------------------------------------------- #
# Lyrics, normalisation, mapping words -> lines
# --------------------------------------------------------------------------- #

@dataclass
class LyricLine:
    text: str
    source_line: int  # line number in the lyrics file (1-based)


@dataclass
class Cue:
    start: float
    end: float
    text: str
    number: int = 0
    source_line: int | None = None
    loss: float | None = None
    srt_end: float | None = None  # end according to the SRT (before hold_ms), used for word timing


BRACKET_LINE = re.compile(r"^\[[^\]]*\]$")


def norm(s: str) -> str:
    """Comparison form: Unicode-normalised, case-folded, letters and digits only.
    Punctuation, hyphens and apostrophes are dropped ("don't" -> "dont")."""
    s = unicodedata.normalize("NFKC", s).casefold()
    return "".join(ch for ch in s if ch.isalnum())


def line_tokens(line: str) -> list[str]:
    """Words of a line (split on whitespace; pure punctuation does not count)."""
    return [t for t in line.split() if norm(t)]


def parse_lyrics(path: Path) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for no, raw in enumerate(read_text_file(path).splitlines(), 1):
        s = raw.strip()
        if not s or BRACKET_LINE.match(s):
            continue
        if not norm(s):
            warn(f"Lyrics line {no} ({s!r}) contains no letters/digits and is skipped.")
            continue
        lines.append(LyricLine(" ".join(s.split()), no))
    return lines


def _api_words(words: list) -> list[dict]:
    """Only words with letters/digits (whitespace/punctuation entries are dropped)."""
    out = []
    for i, w in enumerate(words):
        if not isinstance(w, dict) or "text" not in w:
            raise LyricError(f"Invalid entry in 'words' (position {i}): {w!r}")
        if not norm(str(w["text"])):
            continue
        try:
            float(w["start"]), float(w["end"])
        except (KeyError, TypeError, ValueError):
            raise LyricError(f"Word without a valid start/end time in the response (position {i}): {w!r}")
        out.append(w)
    return out


def map_words_to_lines(lines: list[LyricLine], words: list) -> list[list[dict]]:
    """Assigns the words of the API response to the lyric lines in order.

    The sequence of letters/digits is compared, so punctuation, apostrophes and hyphens do not
    matter, even if the API splits e.g. "rock-n-roll" into several words.
    If something does not match exactly, nothing is guessed – the run is aborted.
    """
    api = _api_words(words)
    keys = [norm(str(w["text"])) for w in api]
    expected_total = sum(len(line_tokens(l.text)) for l in lines)
    groups: list[list[dict]] = []
    pos = 0
    for idx, line in enumerate(lines):
        target = norm(line.text)
        start = pos
        acc = ""
        while len(acc) < len(target) and pos < len(api):
            acc += keys[pos]
            pos += 1
        if acc != target:
            raise AlignmentMismatch(_mismatch_message(lines, idx, api, start, acc, target, expected_total))
        groups.append(api[start:pos])
    if pos < len(api):
        extra = " ".join(repr(str(w["text"])) for w in api[pos:pos + 8])
        raise AlignmentMismatch(
            "The response contains more words than your lyrics.\n"
            f"  Your lyrics: {expected_total} words, response: {len(api)} words.\n"
            f"  After the last lyrics line ({lines[-1].source_line}: \"{lines[-1].text}\") there is still: {extra}\n"
            "  Nothing was guessed and no SRT was written."
        )
    return groups


def _mismatch_message(lines, idx, api, start, acc, target, expected_total) -> str:
    line = lines[idx]
    n = len(line_tokens(line.text))
    got = api[start:start + n + 3]
    got_txt = " ".join(repr(str(w["text"])) for w in got) or "(no words left)"
    common = 0
    while common < min(len(acc), len(target)) and acc[common] == target[common]:
        common += 1
    count_hint = (" (small differences are normal when the API splits e.g. \"Rock-n-Roll\" into several words)"
                  if expected_total != len(api) else "")
    return (
        "The words in the response do not match your lyrics.\n"
        f"  Your lyrics: {expected_total} words, response: {len(api)} words{count_hint}.\n"
        f"  First difference in lyrics line {line.source_line} (SRT entry {idx + 1}):\n"
        f"    Expected ({n} words): \"{line.text}\"\n"
        f"    Response at this point: {got_txt}\n"
        f"    Comparison (letters/digits only): expected '{target[common:common + 25]}…', "
        f"got '{acc[common:common + 25]}…' from character {common + 1} of the line\n"
        "  Nothing was guessed and no SRT was written.\n"
        "  The full response is stored in the alignment.json and can be reused."
    )


def build_cues(lines: list[LyricLine], groups: list[list[dict]], cfg: dict) -> tuple[list[Cue], list[str]]:
    cues: list[Cue] = []
    stretched = []
    limit = float(cfg["quality"].get("max_first_word_seconds", 3.0))
    for i, (line, ws) in enumerate(zip(lines, groups), 1):
        losses = [float(w["loss"]) for w in ws if isinstance(w.get("loss"), (int, float))]
        start = float(ws[0]["start"])
        first_end = float(ws[0]["end"])
        # The alignment sometimes stretches the first word of a line over an intro or a pause.
        # In that case the line starts shortly before the end of that word (estimated word length).
        if first_end - start > limit:
            est = min(1.0, max(0.3, 0.12 * len(norm(str(ws[0]["text"])))))
            stretched.append(f"Entry {i}: first word \"{ws[0]['text']}\" was {fmt_num(first_end - start, 1)} s long "
                             f"(probably intro/pause), start set to {fmt_srt_time(first_end - est)} – please check.")
            start = first_end - est
        cues.append(Cue(
            start=start, end=float(ws[-1]["end"]), text=line.text, number=i,
            source_line=line.source_line, loss=(sum(losses) / len(losses)) if losses else None,
        ))
    notes = fix_overlaps(cues, cfg["alignment"].get("line_hold_ms", 0) / 1000.0)
    return cues, stretched + notes


def fix_overlaps(cues: list[Cue], hold: float = 0.0) -> list[str]:
    """Optionally extends by 'hold' and minimally shortens lines that run into the next one."""
    notes = []
    for i, c in enumerate(cues):
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        if hold > 0:
            c.end = c.end + hold if nxt is None else max(c.end, min(c.end + hold, nxt.start))
        if c.end < c.start:
            c.end = c.start
        if nxt is not None and c.end > nxt.start:
            if nxt.start < c.start:
                notes.append(f"Entry {c.number} starts after entry {nxt.number} – check the order!")
                continue
            cut = c.end - nxt.start
            c.end = nxt.start
            notes.append(f"Entry {c.number} shortened by {fmt_num(cut * 1000, 0)} ms (ran into entry {nxt.number}).")
    return notes


def quality_report(cues: list[Cue], cfg: dict, notes: list[str], audio_duration: float | None,
                   overall_loss: float | None) -> None:
    q = cfg["quality"]
    losses = [c.loss for c in cues if c.loss is not None]
    med = statistics.median(losses) if losses else 0.0
    loss_limit = med * float(q.get("loss_factor", 2.0)) if med > 0 else None
    found = []
    for c in cues:
        d = c.end - c.start
        reasons = []
        if d < q["min_line_seconds"]:
            reasons.append(f"very short ({fmt_num(d)} s)")
        if d > q["max_line_seconds"]:
            reasons.append(f"very long ({fmt_num(d, 1)} s)")
        if loss_limit is not None and c.loss is not None and c.loss > loss_limit:
            reasons.append(f"poor match (loss {fmt_num(c.loss, 3)}, median {fmt_num(med, 3)})")
        if audio_duration and c.end > audio_duration + 0.05:
            reasons.append("ends after the audio")
        if reasons:
            found.append((c, reasons))
    info()
    info("Quality report")
    info("--------------")
    if overall_loss is not None:
        info(f"Overall API loss: {fmt_num(overall_loss, 3)} (lower is better)")
    if not found:
        info("No suspicious lines.")
    for c, reasons in found:
        info(f"  Entry {c.number:>3} (lyrics line {c.source_line}, {fmt_srt_time(c.start)}): "
             f"{'; '.join(reasons)}\n        \"{c.text}\"")
    for n in notes:
        info(f"  {n}")
    # Large gaps: often something is sung there that is missing from the lyrics file (common with Suno)
    max_gap = float(q.get("max_gap_seconds", 20.0))
    edges = [(None, cues[0], cues[0].start)] if cues else []
    edges += [(a, b, b.start - a.end) for a, b in zip(cues, cues[1:])]
    if cues and audio_duration:
        edges.append((cues[-1], None, audio_duration - cues[-1].end))
    for a, b, gap in edges:
        if gap > max_gap:
            where = (f"before entry {b.number}" if a is None else
                     f"after the last entry {a.number}" if b is None else
                     f"between entry {a.number} and {b.number}")
            t_from = fmt_srt_time(a.end) if a else fmt_srt_time(0)
            t_to = fmt_srt_time(b.start) if b else fmt_srt_time(audio_duration)
            info(f"  GAP {where}: {t_from} to {t_to} ({fmt_num(gap, 0)} s without lyrics). If there is singing, "
                 "that text is missing from the lyrics file – add it and align again. For an instrumental part this is fine.")


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #

def fmt_srt_time(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


SRT_TIME = re.compile(r"^\s*(\d+):(\d{1,2}):(\d{1,2})(?:[,.](\d{1,3}))?\s*$")


def parse_srt_time(s: str) -> float | None:
    m = SRT_TIME.match(s)
    if not m:
        return None
    h, mi, se, frac = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int((frac or "0").ljust(3, "0")[:3]) / 1000


def write_srt(path: Path, cues: list[Cue]) -> None:
    blocks = [f"{i}\n{fmt_srt_time(c.start)} --> {fmt_srt_time(c.end)}\n{c.text}\n" for i, c in enumerate(cues, 1)]
    path.write_text("\n".join(blocks), encoding="utf-8")


def parse_srt(path: Path) -> list[Cue]:
    text = read_text_file(path).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        raise LyricError(f"The SRT file {rel(path)} is empty.")
    cues: list[Cue] = []
    for bno, block in enumerate(re.split(r"\n[ \t]*\n", text), 1):
        rows = block.split("\n")
        ti = next((k for k, r in enumerate(rows[:3]) if "-->" in r), None)
        if ti is None:
            raise LyricError(f"SRT {rel(path)}, block {bno}: no timestamp (\"... --> ...\") found:\n  {rows[0]!r}")
        a, _, b = rows[ti].partition("-->")
        start, end = parse_srt_time(a), parse_srt_time(b.split()[0] if b.split() else "")
        if start is None or end is None:
            raise LyricError(f"SRT {rel(path)}, block {bno}: invalid timestamp {rows[ti]!r} "
                             "(format: 00:01:23,450 --> 00:01:25,000)")
        if end < start:
            raise LyricError(f"SRT {rel(path)}, block {bno}: end is before start ({rows[ti].strip()}).")
        body = "\n".join(r.strip() for r in rows[ti + 1:] if r.strip())
        if body:
            cues.append(Cue(start, end, body, number=bno, srt_end=end))
    if not cues:
        raise LyricError(f"No subtitles found in {rel(path)}.")
    return cues


# --------------------------------------------------------------------------- #
# Alignment backends
# --------------------------------------------------------------------------- #

def _explain_api_error(resp, api_key: str) -> str:
    status = resp.status_code
    code, msg = "", ""
    try:
        body = resp.json()
        detail = body.get("detail", body) if isinstance(body, dict) else body
        if isinstance(detail, dict):
            code = str(detail.get("status") or detail.get("code") or detail.get("type") or "")
            msg = str(detail.get("message") or detail.get("msg") or "")
        elif isinstance(detail, list):
            msg = "; ".join(f"{'/'.join(map(str, d.get('loc', [])))}: {d.get('msg', '')}"
                            for d in detail if isinstance(d, dict))
        else:
            msg = str(detail)
    except ValueError:
        msg = resp.text[:300]
    msg = msg.replace(api_key, "***")
    low = f"{code} {msg}".lower()
    # Order matters: ElevenLabs also reports missing credits with HTTP 401
    if status == 402 or "quota" in low or "credit" in low or "insufficient" in low or "payment" in low:
        text = "Your ElevenLabs credits are not sufficient for this request or have been used up."
    elif "missing_permission" in low or (status in (401, 403) and "permission" in low):
        text = ("The API key lacks the permission for Speech to Text / Forced Alignment.\n"
                "  In ElevenLabs go to Developers > API Keys and allow 'Speech to Text' for the key.")
    elif status == 401 or "invalid_api_key" in low or "api key" in low:
        text = "The API key was rejected (invalid or revoked). Please check ELEVENLABS_API_KEY in .env."
    elif status == 413 or "too large" in low or "file size" in low:
        text = "The audio file is too large for the API. Tip: export it as MP3 (see README)."
    elif status == 429:
        text = "Too many concurrent requests (HTTP 429). Please wait a moment and try again."
    elif status == 422:
        text = "The API rejected the request as invalid (validation error)."
    elif status >= 500:
        text = f"Server error at ElevenLabs (HTTP {status}). Please try again later."
    else:
        text = f"The ElevenLabs API reported an error (HTTP {status})."
    details = ", ".join(x for x in (code, msg) if x)
    return f"{text}\n  API response: HTTP {status}" + (f" – {details}" if details else "")


def elevenlabs_align(audio: Path, text: str) -> dict:
    try:
        import requests
    except ImportError:
        raise LyricError("The package 'requests' is missing. Install it in the venv: venv/Scripts/pip install requests")
    api_key = load_api_key()
    size = audio.stat().st_size
    if size >= ELEVENLABS_MAX_BYTES:
        raise LyricError(f"The audio file is too large ({fmt_num(size / 1024 ** 2, 0)} MB). ElevenLabs only accepts "
                         "files under 1 GB for forced alignment. Tip: export it as MP3 (see README).")
    if len(text) > ELEVENLABS_MAX_CHARS:
        raise LyricError(f"The lyrics are too long ({len(text)} characters, allowed: {ELEVENLABS_MAX_CHARS}).")
    mime = mimetypes.guess_type(audio.name)[0] or "application/octet-stream"
    info(f"Sending {audio.name} ({fmt_num(size / 1024 ** 2, 1)} MB) and the lyrics to ElevenLabs (forced alignment) …")
    t0 = time.time()
    try:
        with audio.open("rb") as fh:
            resp = requests.post(
                ELEVENLABS_URL,
                headers={"xi-api-key": api_key, "Accept": "application/json"},
                data={"text": text},
                files={"file": (audio.name, fh, mime)},
                timeout=(20, 1800),
            )
    except requests.exceptions.Timeout:
        raise LyricError("Timeout while connecting to ElevenLabs. Please check your internet connection and try again.")
    except requests.exceptions.SSLError:
        raise LyricError("SSL error while connecting to api.elevenlabs.io (proxy/antivirus?).")
    except requests.exceptions.ConnectionError:
        raise LyricError("No connection to api.elevenlabs.io. Please check your internet connection, proxy or firewall.")
    except requests.exceptions.RequestException as e:
        raise LyricError(f"Network error during the ElevenLabs request ({type(e).__name__}).")
    if resp.status_code != 200:
        raise LyricError(_explain_api_error(resp, api_key))
    try:
        data = resp.json()
    except ValueError:
        raise LyricError("ElevenLabs did not return a valid JSON response.")
    if not isinstance(data, dict) or not isinstance(data.get("words"), list):
        raise LyricError("Unexpected response format from ElevenLabs (field 'words' is missing).")
    info(f"Response received after {fmt_num(time.time() - t0, 1)} s: {len(data['words'])} word entries.")
    return data


def _detect_nvidia() -> tuple[str, tuple[int, int] | None] | None:
    exe = shutil.which("nvidia-smi") or (r"C:\Windows\System32\nvidia-smi.exe"
                                         if Path(r"C:\Windows\System32\nvidia-smi.exe").exists() else None)
    if not exe:
        return None
    try:
        name = subprocess.run([exe, "--query-gpu=name", "--format=csv,noheader"], capture_output=True,
                              text=True, timeout=20).stdout.strip().splitlines()
        full = subprocess.run([exe], capture_output=True, text=True, timeout=20).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if not name:
        return None
    m = re.search(r"CUDA Version:\s*(\d+)\.(\d+)", full)
    return name[0], ((int(m.group(1)), int(m.group(2))) if m else None)


def ensure_local_packages(need_demucs: bool) -> None:
    missing = []
    if importlib.util.find_spec("stable_whisper") is None:
        missing.append("stable-ts")
    if need_demucs and (importlib.util.find_spec("demucs") is None or importlib.util.find_spec("soundfile") is None):
        missing.append("demucs")
    if not missing:
        return
    if sys.prefix == sys.base_prefix:
        raise LyricError("The local backend is only installed into this folder's venv, but the venv is missing. See README.")
    info("Missing for the local backend: " + ", ".join(missing))
    info("Everything is installed into this folder's venv only (PyTorch, stable-ts"
         + (", demucs" if "demucs" in missing else "") + ").")
    if not ask_yes_no("Check and install now?", False):
        raise LyricError("Local backend not installed. Without --backend local, ElevenLabs is used.")
    have_torch = importlib.util.find_spec("torch") is not None
    index = None
    if not have_torch:
        gpu = _detect_nvidia()
        if gpu:
            name, cuda = gpu
            if cuda is None or cuda >= (13, 0):
                index = "cu130" if cuda else "cu128"
            elif cuda >= (12, 8):
                index = "cu128"
            elif cuda >= (12, 6):
                index = "cu126"
            elif cuda >= (11, 8):
                index = "cu118"
            if index:
                info(f"NVIDIA GPU found: {name} (driver supports CUDA {'.'.join(map(str, cuda)) if cuda else '?'}).")
                info(f"PyTorch with CUDA ({index}): download approx. 2.5–3.5 GB, uses approx. 5–6 GB in venv/.")
            else:
                info(f"NVIDIA GPU {name} found, but the driver is too old for current PyTorch versions. "
                     "The CPU version will be installed.")
        else:
            info("No NVIDIA GPU found (nvidia-smi missing). The CPU version will be installed (slower).")
        if not index:
            index = "cpu"
            info("PyTorch (CPU): download approx. 200–300 MB, uses approx. 1 GB in venv/.")
    info("Plus: stable-ts/Whisper approx. 50 MB; on the first run Whisper downloads the model "
         "'medium' (approx. 1.5 GB) to models/whisper/.")
    if "demucs" in missing:
        info("demucs: approx. 50 MB, on the first run also the htdemucs model (approx. 80 MB) to models/torch/.")
    if not ask_yes_no("Start download?", False):
        raise LyricError("Installation cancelled.")
    pip = [sys.executable, "-m", "pip", "install", "--no-cache-dir"]
    steps = []
    if not have_torch:
        steps.append(pip + ["torch", "torchaudio", "--index-url", f"https://download.pytorch.org/whl/{index}"])
    if "stable-ts" in missing:
        steps.append(pip + ["stable-ts"])
    if "demucs" in missing:
        steps.append(pip + ["demucs", "soundfile"])
    for cmd in steps:
        info("> " + " ".join(cmd[2:]))
        if subprocess.run(cmd).returncode != 0:
            raise LyricError("The installation failed (see messages above).")
    importlib.invalidate_caches()


def separate_vocals(audio: Path, out: Path) -> Path:
    """Separates the vocal track with demucs (used for alignment only)."""
    if out.exists() and out.stat().st_mtime >= audio.stat().st_mtime:
        info(f"Using existing vocal track {rel(out)}.")
        return out
    os.environ.setdefault("TORCH_HOME", str(MODELS_DIR / "torch"))
    import torch
    import soundfile as sf
    from demucs.apply import apply_model
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model

    device = "cuda" if torch.cuda.is_available() else "cpu"
    info(f"Separating vocals with demucs (htdemucs, {device}) … this can take a few minutes.")
    model = get_model("htdemucs")
    model.eval()
    wav = AudioFile(audio).read(streams=0, samplerate=model.samplerate, channels=model.audio_channels)
    ref = wav.mean(0)
    mean, std = ref.mean(), ref.std() + 1e-8
    with torch.no_grad():
        sources = apply_model(model, ((wav - mean) / std)[None], device=device, split=True,
                              overlap=0.25, progress=True)[0]
    vocals = sources[model.sources.index("vocals")] * std + mean
    sf.write(str(out), vocals.cpu().numpy().T, model.samplerate)
    info(f"Vocal track saved: {rel(out)}")
    return out


def local_align(audio: Path, text: str, language: str, cfg: dict) -> dict:
    os.environ.setdefault("TORCH_HOME", str(MODELS_DIR / "torch"))
    import torch
    import stable_whisper

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model_name = cfg["local"].get("whisper_model", "medium")
    info(f"Local alignment with stable-ts (Whisper '{model_name}', {device}, language {language}) …")
    model = stable_whisper.load_model(model_name, device=device, download_root=str(MODELS_DIR / "whisper"))
    result = model.align(str(audio), text, language=language, original_split=True)
    if result is None:
        raise LyricError("stable-ts could not align the lyrics.")
    words = []
    for seg in result.segments:
        for w in seg.words:
            prob = getattr(w, "probability", None)
            words.append({"text": w.word.strip(), "start": round(float(w.start), 3), "end": round(float(w.end), 3),
                          "loss": round(1.0 - float(prob), 4) if prob is not None else 0.0})
    losses = [w["loss"] for w in words]
    return {"characters": [], "words": words, "loss": (sum(losses) / len(losses)) if losses else 0.0}


# --------------------------------------------------------------------------- #
# Projects and input files
# --------------------------------------------------------------------------- #

INPUT_DIR = ROOT / "input"
KIND_EXT = {
    "audio": {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"},
    "image": {".png", ".jpg", ".jpeg", ".webp", ".bmp"},
    "text": {".txt"},
}
KIND_LABEL = {"audio": "audio file", "image": "image", "text": "lyrics file (.txt)"}
RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

# Folder/file names used by projects created with older versions; still read so that those keep working.
LEGACY_SOURCE_DIR = "quelle"
LEGACY_INFO_FILE = "projekt.json"


@dataclass
class Project:
    """One project = one folder output/<name>/ with all results, plus source/ with the input files."""
    name: str

    @property
    def dir(self) -> Path:
        return OUTPUT_DIR / self.name

    @property
    def src_dir(self) -> Path:
        new, old = self.dir / "source", self.dir / LEGACY_SOURCE_DIR
        return old if old.is_dir() and not new.exists() else new

    def file(self, suffix: str) -> Path:
        return self.dir / f"{self.name}{suffix}"

    @property
    def srt(self) -> Path:
        return self.file(".srt")

    @property
    def alignment(self) -> Path:
        return self.file(".alignment.json")

    @property
    def info_file(self) -> Path:
        new, old = self.dir / "project.json", self.dir / LEGACY_INFO_FILE
        return old if old.is_file() and not new.exists() else new


def safe_project_name(s: str) -> str:
    """Removes characters that are not allowed in Windows folder names."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", s or "").strip().rstrip(". ")
    if s.upper() in RESERVED_NAMES:
        s = "_" + s
    return s


def files_of_kind(folder: Path, kind: str) -> list[Path]:
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir()
                  if p.is_file() and not p.name.startswith(".") and p.suffix.lower() in KIND_EXT[kind])


def pick_file(folder: Path, kind: str, explicit: str | None = None) -> Path:
    """Finds the file of a given type in the folder – the name does not matter. Asks if there are several."""
    if explicit:
        return require_file(explicit, KIND_LABEL[kind])
    files = files_of_kind(folder, kind)
    if not files:
        raise LyricError(f"No {KIND_LABEL[kind]} found in {rel(folder)}/ "
                         f"(allowed: {', '.join(sorted(KIND_EXT[kind]))}).")
    if len(files) == 1:
        return files[0]
    info(f"{rel(folder)}/ contains several files of type {KIND_LABEL[kind]}:")
    for i, f in enumerate(files, 1):
        info(f"  [{i}] {f.name}")
    while True:
        ans = ask_input("Which one should be used? Enter a number: ")
        if not ans:
            raise LyricError(f"Nothing selected. Remove the extra files from {rel(folder)}/.")
        if ans.isdigit() and 1 <= int(ans) <= len(files):
            return files[int(ans) - 1]
        info(f"Please enter a number from 1 to {len(files)}.")


def ask_project_name(suggestion: str) -> str:
    suggestion = safe_project_name(suggestion) or "project"
    while True:
        name = safe_project_name(ask_input(f"Project name (Enter = \"{suggestion}\"): ", suggestion))
        if name:
            return name
        info("Please enter a valid name (no characters like \\ / : * ? \" < > |).")


def create_project(name_arg: str | None, suggestion: str) -> Project:
    """Creates a new project (or reuses an existing one after asking)."""
    name = safe_project_name(name_arg) if name_arg else ask_project_name(suggestion)
    while True:
        if not name:
            name = ask_project_name(suggestion)
        proj = Project(name)
        if proj.dir.is_dir() and any(proj.dir.iterdir()):
            if ask_yes_no(f"The project \"{name}\" already exists ({rel(proj.dir)}). Reuse it?\n"
                          "(Existing files are only overwritten after asking.) 'n' = choose another name.", True):
                break
            name = ""
            continue
        break
    proj.dir.mkdir(parents=True, exist_ok=True)
    info(f"Project folder: {rel(proj.dir)}")
    return proj


def list_projects() -> list[Path]:
    if not OUTPUT_DIR.is_dir():
        return []
    dirs = [d for d in OUTPUT_DIR.iterdir() if d.is_dir() and any(d.glob("*.srt"))]
    return sorted(dirs, key=lambda d: max(p.stat().st_mtime for p in d.glob("*.srt")), reverse=True)


def choose_project() -> Project:
    projects = list_projects()
    if not projects:
        raise LyricError("There is no project in output/ yet. Run 'all' or 'align' first.")
    info("Existing projects (most recently edited first):")
    for i, d in enumerate(projects, 1):
        info(f"  [{i}] {d.name}")
    ans = ask_input("Which project? Number or name (Enter = 1): ", "1")
    if ans.isdigit() and 1 <= int(ans) <= len(projects):
        return Project(projects[int(ans) - 1].name)
    proj = Project(safe_project_name(ans))
    if proj.dir.is_dir():
        return proj
    raise LyricError(f"The project \"{ans}\" does not exist.")


def open_project(args) -> Project:
    """Project for preview/render: --project, otherwise derived from --srt, otherwise chosen from a list."""
    if args.project:
        proj = Project(safe_project_name(args.project))
        if not proj.dir.is_dir():
            names = ", ".join(d.name for d in list_projects()) or "(none)"
            raise LyricError(f"The project \"{args.project}\" does not exist ({rel(proj.dir)}). Available: {names}")
        return proj
    if args.srt:
        return create_project(None, Path(args.srt).stem)
    return choose_project()


def _load_project_info(proj: Project) -> dict:
    if proj.info_file.exists():
        try:
            return json.loads(read_text_file(proj.info_file))
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_project_info(proj: Project, data: dict) -> None:
    proj.info_file.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def import_sources(proj: Project, sources: dict[str, Path]) -> dict[str, Path]:
    """Copies the input files to output/<project>/source/ and records them in project.json.
    That way the project can be re-rendered later, even if input/ already holds something else."""
    proj.src_dir.mkdir(parents=True, exist_ok=True)
    data = _load_project_info(proj)
    result = {}
    for kind, f in sources.items():
        dest = proj.src_dir / f.name
        if f.resolve() != dest.resolve():
            same = dest.exists() and dest.stat().st_size == f.stat().st_size and sha256_file(dest) == sha256_file(f)
            if not same:
                if dest.exists() and not confirm_overwrite([dest]):
                    raise LyricError("Cancelled, the file in the project stays unchanged.")
                shutil.copy2(f, dest)
        result[kind] = dest
        data[kind] = dest.relative_to(proj.dir).as_posix()
    data.setdefault("created", datetime.now().isoformat(timespec="seconds"))
    save_project_info(proj, data)
    info("Source files in the project: " + ", ".join(f"{KIND_LABEL[k]} {p.name}" for k, p in result.items()))
    return result


def project_style(proj: Project, explicit: str | None, cfg: dict) -> str:
    """Style: --style, otherwise the one last used for this project, otherwise default_style from config.json."""
    if explicit:
        return explicit
    stored = _load_project_info(proj).get("style")
    if stored and stored in load_styles():
        info(f"Project style: \"{stored}\" (change with --style NAME)")
        return stored
    return cfg["default_style"]


def remember_style(proj: Project, style_name: str) -> None:
    data = _load_project_info(proj)
    data["style"] = style_name
    save_project_info(proj, data)


def project_source(proj: Project, kind: str, explicit: str | None) -> Path:
    """Input file of a project: --option, otherwise from project.json, otherwise from source/, otherwise from input/."""
    if explicit:
        return require_file(explicit, KIND_LABEL[kind])
    stored = _load_project_info(proj).get(kind)
    if stored and (proj.dir / stored).is_file():
        return proj.dir / stored
    if files_of_kind(proj.src_dir, kind):
        return pick_file(proj.src_dir, kind)
    found = pick_file(INPUT_DIR, kind)
    return import_sources(proj, {kind: found})[kind]


def find_cached_alignment(proj: Project, audio_hash: str, text_key: str, backend: str,
                          vocals: bool) -> tuple[Path, dict] | None:
    """Looks for a matching stored API response – first in the project, then in all other projects."""
    candidates = [proj.alignment, *sorted(OUTPUT_DIR.glob("*/*.alignment.json")), *sorted(OUTPUT_DIR.glob("*.alignment.json"))]
    seen = set()
    for p in candidates:
        if not p.is_file() or p.resolve() in seen:
            continue
        seen.add(p.resolve())
        try:
            data = json.loads(read_text_file(p))
        except (json.JSONDecodeError, OSError):
            continue
        meta = data.get("_lyricvideo", {}) if isinstance(data, dict) else {}
        if (meta.get("audio_sha256") == audio_hash and meta.get("text_key") == text_key
                and meta.get("backend") == backend and bool(meta.get("vocals")) == vocals
                and isinstance(data.get("words"), list)):
            return p, data
    return None


# --------------------------------------------------------------------------- #
# align
# --------------------------------------------------------------------------- #

def do_align(audio: Path, text_file: Path, proj: Project, backend: str, language: str, vocals: bool,
             realign: bool, cfg: dict, keep_existing_srt: bool = False) -> tuple[Path, Path]:
    check_ffmpeg()
    duration = media_duration(audio)
    lines = parse_lyrics(text_file)
    if not lines:
        raise LyricError(f"No sung lines found in {rel(text_file)}.")
    proj.dir.mkdir(parents=True, exist_ok=True)
    srt_path = proj.srt
    align_path = proj.alignment
    vocals_path = proj.file(".vocals.wav")

    sep = cfg["alignment"].get("api_line_separator", "\n")
    sung_text = sep.join(l.text for l in lines)
    n_words = sum(len(line_tokens(l.text)) for l in lines)
    info(f"Lyrics: {len(lines)} sung lines, {n_words} words (empty lines and [sections] are not sent).")
    if backend == "elevenlabs" and language:
        info("Note: the ElevenLabs API has no language parameter, --language only applies to --backend local.")

    audio_hash = sha256_file(audio)
    text_key = sha256_text(norm(sung_text))  # the word sequence counts, not line breaks/punctuation
    response = None
    found_path = None
    found = find_cached_alignment(proj, audio_hash, text_key, backend, vocals)
    if found and realign:
        info("Existing alignment ignored because of --realign.")
    elif found:
        where = "in this project" if found[0] == align_path else f"in {rel(found[0])}"
        if ask_yes_no(f"There is already an alignment for this audio and these lyrics ({where}).\n"
                      "Use it (saves API credits)? 'n' calls the API again.", True):
            found_path, response = found
    elif align_path.exists():
        info(f"The existing {rel(align_path)} belongs to different audio, lyrics or backend.")
    if found_path is not None and found_path != align_path:
        # Copy the response from another project into the current one (needed for karaoke/reveal when rendering)
        if align_path.exists() and not confirm_overwrite([align_path]):
            raise LyricError("Cancelled, the existing alignment.json stays unchanged.")
        align_path.write_text(json.dumps(response, ensure_ascii=False, indent=1), encoding="utf-8")
        info(f"Alignment taken from {rel(found_path)}")

    if srt_path.exists() and not confirm_overwrite(
            [srt_path], "Caution: you may already have edited this SRT."):
        if keep_existing_srt:
            info(f"Keeping the existing SRT: {rel(srt_path)}")
            return srt_path, align_path
        raise LyricError("Cancelled, nothing was overwritten. (Use --yes to overwrite without asking.)")

    if response is None:
        if align_path.exists() and not confirm_overwrite([align_path]):
            raise LyricError("Cancelled, the existing alignment.json stays unchanged.")
        if backend == "local":
            ensure_local_packages(vocals)
            src = separate_vocals(audio, vocals_path) if vocals else audio
            response = local_align(src, "\n".join(l.text for l in lines), language, cfg)
        else:
            if vocals:
                ensure_local_packages(True)
                src = separate_vocals(audio, vocals_path)
            else:
                src = audio
            response = elevenlabs_align(src, sung_text)
        response["_lyricvideo"] = {
            "info": "Extra data added by lyricvideo.py; all other fields are the unmodified response.",
            "created": datetime.now().isoformat(timespec="seconds"),
            "backend": backend,
            "vocals": vocals,
            "language": language,
            "audio_file": audio.name,
            "audio_sha256": audio_hash,
            "text_key": text_key,
            "lines": [{"text": l.text, "source_line": l.source_line} for l in lines],
        }
        # save immediately so the (paid) response survives later errors
        align_path.write_text(json.dumps(response, ensure_ascii=False, indent=1), encoding="utf-8")
        info(f"API response saved: {rel(align_path)}")
    else:
        info(f"Using stored alignment: {rel(align_path)}")

    groups = map_words_to_lines(lines, response["words"])
    cues, notes = build_cues(lines, groups, cfg)
    write_srt(srt_path, cues)
    info(f"SRT written: {rel(srt_path)} ({len(cues)} entries)")
    quality_report(cues, cfg, notes, duration, response.get("loss") if isinstance(response.get("loss"), (int, float)) else None)
    return srt_path, align_path


# --------------------------------------------------------------------------- #
# Styles
# --------------------------------------------------------------------------- #

BUILTIN_STYLE = {
    "font": "Arial",
    "bold": True,
    "italic": False,
    "size": 64,
    "color": "#FFFFFF",
    "outline_color": "#000000",
    "outline_width": 4,
    "shadow_color": "#000000",
    "shadow_depth": 2,
    "box": False,
    "box_color": "#000000",
    "box_opacity": 0.5,
    "position": "bottom",
    "margin": 70,
    "align": "center",
    "uppercase": False,
    "letter_spacing": 0,
    "max_width_percent": 85,
    "fade_in_ms": 150,
    "fade_out_ms": 150,
    "highlight": "none",
    "highlight_color": "#FFD700",
    "word_fade_ms": 120,
    "hold_ms": 0,
    "background": {"dim_percent": 0},
}

# Accepted values -> canonical value. The German words are legacy values from older versions.
POSITIONS = {"bottom": "bottom", "middle": "middle", "center": "middle", "top": "top",
             "unten": "bottom", "mitte": "middle", "oben": "top"}
ALIGNS = {"left": "left", "center": "center", "centre": "center", "middle": "center", "right": "right",
          "links": "left", "zentriert": "center", "mitte": "center", "rechts": "right"}
HIGHLIGHTS = {"none": "none", "line": "line", "karaoke": "karaoke", "reveal": "reveal",
              "keine": "none", "zeile": "line", "wortweise": "reveal"}
CHOICE_FIELDS = (("position", POSITIONS), ("align", ALIGNS), ("highlight", HIGHLIGHTS))
WORD_MODES = ("karaoke", "reveal")  # need word timings from the alignment.json
WORD_MODE_FALLBACK = {"karaoke": "line", "reveal": "none"}

HEX_COLOR = re.compile(r"^#?([0-9A-Fa-f]{6}|[0-9A-Fa-f]{3})$")


def normalize_choices(values: dict | None) -> dict:
    """Maps position/align/highlight values (including legacy ones) to their canonical value."""
    out = dict(values or {})
    for field, table in CHOICE_FIELDS:
        key = str(out.get(field, "")).strip().lower()
        if key in table:
            out[field] = table[key]
    return out


def load_styles() -> dict:
    if not STYLES_FILE.exists():
        warn("styles.json is missing, using the built-in defaults.")
        return {"standard": {}}
    data = load_json_file(STYLES_FILE, "styles.json")
    styles = {k: v for k, v in data.items() if not k.startswith("_")}
    for k, v in styles.items():
        if not isinstance(v, dict):
            raise LyricError(f"styles.json: preset '{k}' must be an object {{...}}.")
    return styles


def resolve_style(name: str, overrides: dict) -> tuple[dict, dict]:
    """built-in <- 'standard' <- chosen preset <- command line. Returns (style, source per field)."""
    styles = load_styles()
    if name not in styles:
        raise LyricError(f"Style '{name}' does not exist in styles.json. Available: {', '.join(styles) or '(none)'}")
    style = copy.deepcopy(BUILTIN_STYLE)
    source = {k: "built-in" for k in style}
    layers = [("standard", styles.get("standard", {}))]
    if name != "standard":
        layers.append((name, styles[name]))
    layers.append(("command line", overrides))
    for label, layer in layers:
        for k, v in layer.items():
            if k.startswith("_"):
                continue
            if k not in BUILTIN_STYLE:
                warn(f"Unknown style field '{k}' ({label}) is ignored – typo?")
                continue
            if k == "background":
                if v in (None, False):
                    v = {"dim_percent": 0}
                elif isinstance(v, (int, float)):
                    v = {"dim_percent": v}
                elif not isinstance(v, dict):
                    raise LyricError(f"Style field 'background' ({label}) must be an object, e.g. {{\"dim_percent\": 30}}.")
                v = {**style["background"], **v}
            style[k] = v
            source[k] = label
    return validate_style(style, name), source


def validate_style(s: dict, name: str) -> dict:
    def err(field, msg):
        raise LyricError(f"Style '{name}': field '{field}' {msg}")

    for f in ("color", "outline_color", "shadow_color", "box_color", "highlight_color"):
        v = str(s[f]).strip()
        if not HEX_COLOR.match(v):
            err(f, f"must be a hex colour like \"#F5E6C8\" (is: {s[f]!r}).")
        v = v.lstrip("#")
        if len(v) == 3:
            v = "".join(c * 2 for c in v)
        s[f] = "#" + v.upper()
    for f, lo, hi in (("size", 1, 1000), ("outline_width", 0, 100), ("shadow_depth", 0, 100), ("margin", 0, 5000),
                      ("letter_spacing", -50, 200), ("max_width_percent", 10, 100), ("fade_in_ms", 0, 60000),
                      ("fade_out_ms", 0, 60000), ("word_fade_ms", 0, 5000), ("hold_ms", 0, 30000)):
        try:
            s[f] = float(s[f])
        except (TypeError, ValueError):
            err(f, f"must be a number (is: {s[f]!r}).")
        if not lo <= s[f] <= hi:
            err(f, f"must be between {lo} and {hi} (is: {s[f]:g}).")
    try:
        op = float(s["box_opacity"])
    except (TypeError, ValueError):
        err("box_opacity", "must be a number between 0 and 1.")
    s["box_opacity"] = op / 100 if op > 1 else op
    if not 0 <= s["box_opacity"] <= 1:
        err("box_opacity", "must be between 0 and 1 (e.g. 0.5).")
    try:
        dim = float(s["background"].get("dim_percent", 0) or 0)
    except (TypeError, ValueError):
        err("background.dim_percent", "must be a number between 0 and 100.")
    if not 0 <= dim <= 100:
        err("background.dim_percent", "must be between 0 and 100.")
    s["background"]["dim_percent"] = dim
    for f, table in CHOICE_FIELDS:
        key = str(s[f]).strip().lower()
        if key not in table:
            allowed = sorted(set(table.values()))
            err(f, f"has an unknown value {s[f]!r}. Allowed: {', '.join(allowed)}.")
        s[f] = table[key]
    for f in ("box", "uppercase", "bold", "italic"):
        if not isinstance(s[f], bool):
            err(f, "must be true or false.")
    s["font"] = str(s["font"]).replace(",", " ").strip() or "Arial"
    return s


# Fonts ----------------------------------------------------------------------

def _windows_font_names() -> set[str]:
    names: set[str] = set()
    try:
        import winreg
    except ImportError:
        return names
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    value_name = winreg.EnumValue(key, i)[0]
                except OSError:
                    break
                i += 1
                base = re.sub(r"\s*\([^)]*\)\s*$", "", value_name)
                for part in base.split("&"):
                    if part.strip():
                        names.add(part.strip().casefold())
    return names


def _custom_fonts() -> dict[str, str]:
    files = [p for p in FONTS_DIR.glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc", ".otc")]
    out: dict[str, str] = {}
    if not files:
        return out
    try:
        from fontTools.ttLib import TTCollection, TTFont
    except ImportError:
        warn("Package 'fonttools' is missing, fonts in the fonts/ folder cannot be checked.")
        return out
    for p in files:
        try:
            fonts = TTCollection(str(p)).fonts if p.suffix.lower() in (".ttc", ".otc") else [TTFont(str(p), lazy=True)]
            for f in fonts:
                for rec in f["name"].names:
                    if rec.nameID in (1, 4, 16):
                        try:
                            out.setdefault(rec.toUnicode().strip().casefold(), p.name)
                        except Exception:
                            pass
        except Exception as e:
            warn(f"Font file fonts/{p.name} could not be read ({e}).")
    return out


def resolve_font(name: str) -> tuple[str, str]:
    """Checks whether the font exists. Returns (font name, where it was found)."""
    key = name.casefold()
    custom = _custom_fonts()
    if key in custom:
        return name, f"fonts/{custom[key]}"
    win = _windows_font_names()
    if key in win or any(n.startswith(key + " ") for n in win):
        return name, "Windows"
    print("\n" + "!" * 70, flush=True)
    warn(f"The font '{name}' was found neither in Windows nor in the fonts/ folder.\n"
         "         'Arial' is used instead. Check the font name (as shown in Windows)\n"
         "         or put the .ttf/.otf file into the fonts/ folder.")
    print("!" * 70 + "\n", flush=True)
    return "Arial", "fallback"


# --------------------------------------------------------------------------- #
# ASS generation
# --------------------------------------------------------------------------- #

def ass_color(hex_color: str, alpha: int = 0) -> str:
    """'#RRGGBB' -> '&HAABBGGRR' (ASS stores blue-green-red, alpha 00 = opaque)."""
    v = hex_color.lstrip("#")
    r, g, b = v[0:2], v[2:4], v[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def ass_escape(s: str) -> str:
    return s.replace("\\", "\u29F5").replace("{", "(").replace("}", ")")


def _display(s: str, style: dict) -> str:
    return s.upper() if style["uppercase"] else s


def match_cues_to_words(cues: list[Cue], words: list) -> list[list[dict] | None]:
    """Finds the matching words of the alignment.json for every SRT entry.
    For repetitions (chorus) the occurrence closest in time wins."""
    api = _api_words(words)
    keys = [norm(str(w["text"])) for w in api]
    result: list[list[dict] | None] = []
    for c in cues:
        target = norm(c.text)
        best = None
        if target:
            for i in range(len(api)):
                if not target.startswith(keys[i]):
                    continue
                acc, j = "", i
                while len(acc) < len(target) and j < len(api):
                    acc += keys[j]
                    j += 1
                if acc == target:
                    dist = abs(float(api[i]["start"]) - c.start)
                    if best is None or dist < best[0]:
                        best = (dist, i, j)
        result.append(api[best[1]:best[2]] if best else None)
    return result


def _word_segments(cue: Cue, start: float, end: float, words: list[dict], style: dict) -> list[tuple[float, str]]:
    """Splits the displayed text into words with their start time (seconds, within start..end).
    If you moved the whole line in the SRT (start and end by roughly the same amount), the word
    timings move with it. If you only corrected the start (e.g. first word stretched over the intro),
    the real word timings are kept. Pure punctuation sticks to the previous word."""
    start_off = start - float(words[0]["start"])
    end_off = (cue.srt_end if cue.srt_end is not None else end) - float(words[-1]["end"])
    offset = start_off if abs(start_off - end_off) <= 0.3 else 0.0
    char_times: list[float] = []
    for w in words:
        char_times += [float(w["start"]) + offset] * len(norm(str(w["text"])))
    segments: list[list] = []  # [time, text]
    ci = 0
    rows = cue.text.split("\n")
    for ri, row in enumerate(rows):
        toks = row.split()
        for ti, tok in enumerate(toks):
            sep = " " if ti < len(toks) - 1 else ("\\N" if ri < len(rows) - 1 else "")
            disp = ass_escape(_display(tok, style)) + sep
            n = len(norm(tok))
            if n:
                t = char_times[ci] if ci < len(char_times) else end
                ci += n
                segments.append([t, disp])
            elif segments:
                segments[-1][1] += disp
            else:
                segments.append([start, disp])
    result = []
    prev = start
    for t, disp in segments:
        t = min(max(t, start, prev), end)
        result.append((t, disp))
        prev = t
    return result


def karaoke_text(cue: Cue, start: float, end: float, words: list[dict], style: dict) -> str:
    """Text with \\k tags (centiseconds): every word changes colour while it is sung."""
    segments = _word_segments(cue, start, end, words, style)
    out = []
    cs = [int(round((t - start) * 100)) for t, _ in segments]
    total = int(round((end - start) * 100))
    if cs and cs[0] > 0:
        out.append(f"{{\\k{cs[0]}}}")
    for i, (_, disp) in enumerate(segments):
        nxt = cs[i + 1] if i + 1 < len(cs) else max(total, cs[i])
        out.append(f"{{\\k{max(0, nxt - cs[i])}}}{disp}")
    return "".join(out)


def reveal_text(cue: Cue, start: float, end: float, words: list[dict], style: dict) -> str:
    """Every word is invisible and appears (with a short fade) at the moment it is sung.
    The line is laid out completely from the start, so words do not jump around."""
    fade = int(style["word_fade_ms"])
    out = []
    for t, disp in _word_segments(cue, start, end, words, style):
        ms = int(round((t - start) * 1000))
        if ms <= 0 and fade <= 0:
            out.append(f"{{\\alpha&H00&}}{disp}")
        else:
            out.append(f"{{\\alpha&HFF&\\t({ms},{ms + max(fade, 1)},\\alpha&H00&)}}{disp}")
    return "".join(out)


def build_ass(cues: list[Cue], style: dict, width: int, height: int,
              align_words: list | None) -> tuple[str, str]:
    """Builds the content of the .ass file. Returns (text, highlight mode actually used)."""
    k = min(width, height) / 1080.0  # all pixel values refer to 1080 px on the shorter side
    fs = max(1, round(style["size"] * k))
    ow = round(style["outline_width"] * k, 1)
    sd = round(style["shadow_depth"] * k, 1)
    sp = round(style["letter_spacing"] * k, 1)
    margin = round(style["margin"] * k)
    maxw = width * style["max_width_percent"] / 100.0
    if style["align"] == "center":
        ml = mr = max(margin if style["max_width_percent"] < 100 else 0, round((width - maxw) / 2))
    elif style["align"] == "left":
        ml = margin
        mr = max(0, round(width - ml - maxw))
    else:
        mr = margin
        ml = max(0, round(width - mr - maxw))
    an = {"bottom": 2, "middle": 5, "top": 8}[style["position"]] + {"left": -1, "center": 0, "right": 1}[style["align"]]

    highlight = style["highlight"]
    matched: list[list[dict] | None] = [None] * len(cues)
    if highlight in WORD_MODES:
        if align_words is None:
            highlight = WORD_MODE_FALLBACK[highlight]
        else:
            matched = match_cues_to_words(cues, align_words)
            missing = [c.number for c, m in zip(cues, matched) if m is None]
            if missing:
                warn(f"{highlight}: for SRT entry {', '.join(map(str, missing[:15]))}{' …' if len(missing) > 15 else ''} "
                     "the text does not match the alignment.json – the whole line is shown at once there.")

    text_col = ass_color(style["color"])
    hl_col = ass_color(style["highlight_color"])
    primary, secondary = {"none": (text_col, hl_col), "line": (hl_col, text_col),
                          "karaoke": (hl_col, text_col), "reveal": (text_col, hl_col)}[highlight]
    bold = -1 if style["bold"] else 0
    italic = -1 if style["italic"] else 0
    font = style["font"]
    fmt = ("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, "
           "Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
           "MarginL, MarginR, MarginV, Encoding")
    lyric = (f"Style: Lyric,{font},{fs},{primary},{secondary},{ass_color(style['outline_color'])},"
             f"{ass_color(style['shadow_color'])},{bold},{italic},0,0,100,100,{sp:g},0,1,{ow:g},{sd:g},{an},"
             f"{ml},{mr},{margin},1")
    box_alpha = round((1 - style["box_opacity"]) * 255)
    pad = round(max(ow, fs * 0.25), 1)
    box = (f"Style: Box,{font},{fs},&HFF000000,&HFF000000,{ass_color(style['box_color'], box_alpha)},&HFF000000,"
           f"{bold},{italic},0,0,100,100,{sp:g},0,3,{pad:g},0,{an},{ml},{mr},{margin},1")

    events = []
    for c, words in zip(cues, matched):
        if c.end <= c.start:
            continue
        dur_ms = (c.end - c.start) * 1000
        fi, fo = style["fade_in_ms"], style["fade_out_ms"]
        if fi + fo > dur_ms:
            f = dur_ms / (fi + fo)
            fi, fo = fi * f, fo * f
        fade = f"{{\\fad({int(fi)},{int(fo)})}}" if (fi or fo) else ""
        plain = "\\N".join(ass_escape(_display(r, style)) for r in c.text.split("\n"))
        if highlight == "karaoke" and words:
            body = karaoke_text(c, c.start, c.end, words, style)
        elif highlight == "reveal" and words:
            body = reveal_text(c, c.start, c.end, words, style)
        else:
            body = plain
        times = f"{ass_time(c.start)},{ass_time(c.end)}"
        if style["box"]:
            events.append(f"Dialogue: 0,{times},Box,,0,0,0,,{fade}{plain}")
        events.append(f"Dialogue: 1,{times},Lyric,,0,0,0,,{fade}{body}")

    text = "\n".join([
        "[Script Info]",
        "; generated by lyricvideo.py – the SRT is the source, changes here are overwritten on the next render",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 0",
        "ScaledBorderAndShadow: yes",
        "YCbCr Matrix: TV.709",
        "",
        "[V4+ Styles]",
        fmt,
        lyric,
        box,
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
        *events,
        "",
    ])
    return text, highlight


def load_alignment_words(path: Path | None) -> list | None:
    if path is None:
        return None
    data = load_json_file(path, "alignment.json")
    words = data.get("words")
    if not isinstance(words, list):
        raise LyricError(f"{rel(path)} has no field 'words'.")
    return words


def find_alignment(srt: Path, explicit: str | None, project_alignment: Path | None = None) -> Path | None:
    if explicit:
        return require_file(explicit, "alignment.json")
    for cand in (srt.with_name(srt.stem + ".alignment.json"), project_alignment):
        if cand is None:
            continue
        if cand.exists():
            return cand
    return None


def hold_cues(cues: list[Cue], hold: float) -> None:
    """Keeps every line on screen 'hold' seconds longer after its last word,
    at most until the next line starts (video only, the SRT stays unchanged)."""
    if hold <= 0:
        return
    for i, c in enumerate(cues):
        end = c.end + hold
        if i + 1 < len(cues):
            end = min(end, max(cues[i + 1].start, c.end))
        c.end = end


def prepare_ass(srt: Path, style_name: str, overrides: dict, alignment: str | None, cfg: dict,
                width: int, height: int, project_alignment: Path | None = None) -> tuple[str, dict, dict, list[Cue], str]:
    cues = parse_srt(srt)
    notes = fix_overlaps(cues)
    for n in notes:
        warn(f"SRT: {n} (video only, the SRT stays unchanged)")
    style, source = resolve_style(style_name, overrides)
    hold_cues(cues, style["hold_ms"] / 1000.0)
    style["font"], where = resolve_font(style["font"])
    if where == "fallback":
        source["font"] = "fallback"
    hl = style["highlight"]
    align_path = find_alignment(srt, alignment, project_alignment) if hl in WORD_MODES else None
    if hl in WORD_MODES and align_path is None:
        warn(f"highlight '{hl}' needs the word timings from the alignment.json "
             f"(looked for: {rel(srt.with_name(srt.stem + '.alignment.json'))}). "
             f"Using '{WORD_MODE_FALLBACK[hl]}' instead.")
    ass_text, used = build_ass(cues, style, width, height, load_alignment_words(align_path))
    style["_highlight_used"] = used
    style["_font_where"] = where
    return ass_text, style, source, cues, used


def print_style(style_name: str, style: dict, source: dict, width: int, height: int) -> None:
    k = min(width, height) / 1080.0
    info()
    info(f"Style values used (preset \"{style_name}\" from styles.json, video {width}x{height}):")
    for f in BUILTIN_STYLE:
        v = style[f]
        if f == "background":
            shown = f"{{\"dim_percent\": {v['dim_percent']:g}}}"
        elif isinstance(v, bool):
            shown = "true" if v else "false"
        elif isinstance(v, float):
            shown = f"{v:g}"
        else:
            shown = f"\"{v}\""
        extra = ""
        if f == "size" and k != 1:
            extra = f"  -> {round(v * k)} px in the video"
        if f == "font":
            extra = f"  ({style.get('_font_where')})"
        if f == "highlight" and style.get("_highlight_used") != v:
            extra = f"  -> used: \"{style.get('_highlight_used')}\""
        info(f"  {f:<18} {shown:<26} [{source.get(f, '?')}]{extra}")
    info("[Brackets] show where a value comes from. To change it permanently, edit the preset in styles.json.")


# --------------------------------------------------------------------------- #
# preview / render
# --------------------------------------------------------------------------- #

def make_preview(image: Path, srt: Path, audio: Path | None, proj: Project, style_name: str, overrides: dict, cfg: dict,
                 at: float | None, clip: float | None, lowres: bool, alignment: str | None,
                 open_after: bool | None) -> Path:
    check_ffmpeg()
    t_start = time.time()
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    ass_text, style, source, cues, _ = prepare_ass(srt, style_name, overrides, alignment, cfg, W, H, proj.alignment)
    if at is None:
        at = (cues[0].start + cues[0].end) / 2
    factor = float(cfg["preview"].get("lowres_factor", 0.5)) if lowres else 1.0
    pw, ph = max(2, int(W * factor / 2) * 2), max(2, int(H * factor / 2) * 2)
    out_dir = proj.dir
    out_dir.mkdir(parents=True, exist_ok=True)
    ass_path = out_dir / "preview.ass"
    ass_path.write_text(ass_text, encoding="utf-8-sig")
    vf_base = base_video_filter(pw, ph, style["background"]["dim_percent"])
    shift = f"settb=1/1000,setpts=PTS+{at:.3f}/TB"
    sub = ass_filter(ass_path.name, out_dir)

    if clip:
        if audio is None:
            raise LyricError("--clip needs --audio.")
        dur_audio = media_duration(audio)
        if dur_audio and at >= dur_audio:
            raise LyricError(f"--time {fmt_num(at, 1)} is after the end of the audio ({fmt_num(dur_audio, 1)} s).")
        out = out_dir / "preview.mp4"
        run_ffmpeg([
            "-y", *image_input_args(image, fps),
            "-ss", f"{at:.3f}", "-t", f"{clip:.3f}", "-i", str(audio.resolve()),
            "-filter_complex", f"[0:v]{vf_base},{shift},{sub},setpts=PTS-STARTPTS[v]",
            "-map", "[v]", "-map", "1:a", "-t", f"{clip:.3f}", "-r", str(fps),
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "stillimage",
            "-crf", str(cfg["preview"].get("crf", 28)), "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out.name,
        ], cwd=out_dir)
    else:
        out = out_dir / "preview.png"
        run_ffmpeg([
            "-y", *image_input_args(image, 1), "-frames:v", "1",
            "-vf", f"{vf_base},{shift},{sub}", "-update", "1", out.name,
        ], cwd=out_dir)

    active = [c for c in cues if c.start <= at < c.end]
    info()
    if clip:
        info(f"Preview clip from {fmt_num(at, 1)} s ({fmt_num(clip, 1)} s, {pw}x{ph}) done in "
             f"{fmt_num(time.time() - t_start, 1)} s: {rel(out)}")
    else:
        info(f"Preview still at {fmt_num(at, 1)} s ({pw}x{ph}) done in {fmt_num(time.time() - t_start, 1)} s: {rel(out)}")
    if active:
        for c in active:
            info(f"  Active subtitle (entry {c.number}, {fmt_srt_time(c.start)} --> {fmt_srt_time(c.end)}): "
                 f"\"{c.text.replace(chr(10), ' / ')}\"")
    elif not clip:
        nxt = next((c for c in cues if c.start > at), None)
        warn(f"No subtitle is active at {fmt_num(at, 1)} s."
             + (f" Next: entry {nxt.number} from {fmt_num(nxt.start, 1)} s." if nxt else ""))
    print_style(style_name, style, source, W, H)
    if open_after is None:
        open_after = bool(cfg["preview"].get("open_after", True))
    if open_after and hasattr(os, "startfile"):
        try:
            os.startfile(str(out))
        except OSError:
            pass
    return out


def make_render(image: Path, audio: Path, srt: Path, proj: Project, style_name: str, overrides: dict, cfg: dict,
                alignment: str | None, progress=None, trim_end: float | None = None, fade_out: float = 0.0) -> Path:
    """progress(share 0..1) is called while rendering (for the web UI).
    trim_end: end the video at this point (seconds); fade_out: fade picture and sound out for this long before."""
    check_ffmpeg()
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    out_dir = proj.dir
    out_dir.mkdir(parents=True, exist_ok=True)
    out_mp4 = proj.file(".mp4")
    out_ass = proj.file(".ass")
    duration = media_duration(audio)
    ass_text, style, source, cues, used = prepare_ass(srt, style_name, overrides, alignment, cfg, W, H, proj.alignment)
    if duration and cues[-1].end > duration + 0.05:
        warn(f"The last subtitle ends after the audio ({fmt_srt_time(cues[-1].end)} > {fmt_srt_time(duration)}).")
    if not confirm_overwrite([out_mp4, out_ass]):
        raise LyricError("Cancelled, nothing was overwritten. (Use --yes to overwrite without asking.)")
    out_ass.write_text(ass_text, encoding="utf-8-sig")
    # Special characters in the file name would break the filter syntax -> render with a working copy
    tmp_ass = None
    ass_name = out_ass.name
    if not SAFE_FILTER_NAME.match(ass_name):
        tmp_ass = out_dir / "_render_tmp.ass"
        tmp_ass.write_text(ass_text, encoding="utf-8-sig")
        ass_name = tmp_ass.name
    v = cfg["video"]
    length = duration
    trimmed = bool(trim_end and trim_end > 0 and (duration is None or trim_end < duration - 0.05))
    if trimmed:
        length = float(trim_end)
    fade = min(float(fade_out or 0), length or 0) if trimmed else 0.0
    vf = f"{base_video_filter(W, H, style['background']['dim_percent'])},{ass_filter(ass_name, out_dir)}"
    af = None
    if fade > 0:
        vf += f",fade=t=out:st={length - fade:.3f}:d={fade:.3f}"
        af = f"afade=t=out:st={length - fade:.3f}:d={fade:.3f}"
    info(f"Rendering {rel(out_mp4)} ({W}x{H}, {fps} fps, style \"{style_name}\", highlight \"{used}\") …")
    if trimmed:
        info(f"Trimmed: ends at {fmt_srt_time(length)}"
             + (f", the last {fmt_num(fade, 1)} s fade out." if fade else "."))
    remember_style(proj, style_name)
    t0 = time.time()
    try:
        cmd = [
            "-y", "-stats", *image_input_args(image, fps), "-i", str(audio.resolve()),
            "-vf", vf,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-tune", "stillimage", "-preset", str(v.get("preset", "medium")),
            "-crf", str(v.get("crf", 18)), "-pix_fmt", "yuv420p", "-r", str(fps),
            "-c:a", "aac", "-b:a", str(v.get("audio_bitrate", "192k")),
            "-shortest",
        ]
        if af:
            cmd += ["-af", af]
        if length:
            cmd += ["-t", f"{length:.3f}"]
        cmd += ["-movflags", "+faststart", str(out_mp4.resolve())]
        on_time = (lambda sec: progress(min(1.0, sec / length))) if (progress and length) else None
        run_ffmpeg(cmd, cwd=out_dir, progress=on_time)
    finally:
        if tmp_ass and tmp_ass.exists():
            tmp_ass.unlink()
    info(f"Done in {fmt_num(time.time() - t0, 1)} s: {rel(out_mp4)} ({fmt_num(out_mp4.stat().st_size / 1024 ** 2, 1)} MB)")
    info(f"Subtitle file: {rel(out_ass)}")
    return out_mp4


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #

STYLE_ARGS = [
    # (argparse name, style field, type, help)
    ("--font", "font", str, "font name, e.g. \"Palatino Linotype\""),
    ("--size", "size", float, "font size in px (relative to 1080 px on the shorter video side)"),
    ("--color", "color", str, "text colour, e.g. \"#FFD700\""),
    ("--outline-color", "outline_color", str, "outline colour"),
    ("--outline-width", "outline_width", float, "outline width in px"),
    ("--shadow-color", "shadow_color", str, "shadow colour"),
    ("--shadow-depth", "shadow_depth", float, "shadow distance in px"),
    ("--box-color", "box_color", str, "colour of the box behind the text"),
    ("--box-opacity", "box_opacity", float, "opacity of the box 0..1"),
    ("--position", "position", str, "bottom, middle or top"),
    ("--margin", "margin", float, "distance to the edge in px"),
    ("--align", "align", str, "left, center or right"),
    ("--letter-spacing", "letter_spacing", float, "letter spacing in px"),
    ("--max-width-percent", "max_width_percent", float, "maximum text width in %% of the video width"),
    ("--fade-in-ms", "fade_in_ms", float, "fade in, in ms"),
    ("--fade-out-ms", "fade_out_ms", float, "fade out, in ms"),
    ("--highlight", "highlight", str, "none, line, karaoke or reveal (words appear one by one)"),
    ("--highlight-color", "highlight_color", str, "colour for line/karaoke"),
    ("--word-fade-ms", "word_fade_ms", float, "fade-in time per word for reveal, in ms"),
    ("--hold-ms", "hold_ms", float, "keep the line this many ms longer after its last word"),
]
STYLE_FLAGS = [("--box", "box"), ("--uppercase", "uppercase"), ("--bold", "bold"), ("--italic", "italic")]


def add_style_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("style", "values override styles.json for this run only")
    g.add_argument("--style", help="name of the preset in styles.json (default from config.json)")
    for opt, field, typ, hlp in STYLE_ARGS:
        g.add_argument(opt, dest=f"st_{field}", type=typ, help=hlp, metavar="VALUE")
    for opt, field in STYLE_FLAGS:
        g.add_argument(opt, dest=f"st_{field}", action=argparse.BooleanOptionalAction, default=None,
                       help=f"{field} on/off")
    g.add_argument("--dim-percent", dest="st_dim_percent", type=float, help="darken the image in %% (background.dim_percent)")


def style_overrides(args) -> dict:
    out = {}
    for key, val in vars(args).items():
        if key.startswith("st_") and val is not None:
            field = key[3:]
            if field == "dim_percent":
                out["background"] = {"dim_percent": val}
            else:
                out[field] = val
    return out


def _input_files(args, kinds: tuple[str, ...]) -> dict[str, Path]:
    """Input files from input/ (name does not matter) or from the options --audio/--image/--text."""
    files = {k: pick_file(INPUT_DIR, k, getattr(args, k, None)) for k in kinds}
    info("Input files: " + ", ".join(f"{KIND_LABEL[k]} {rel(p)}" for k, p in files.items()))
    return files


def cmd_align(args, cfg) -> None:
    files = _input_files(args, ("audio", "text"))
    proj = create_project(args.project, files["audio"].stem)
    files = import_sources(proj, files)
    srt, _ = do_align(files["audio"], files["text"], proj, args.backend or cfg["default_backend"],
                      args.language or cfg["default_language"], args.vocals, args.realign, cfg)
    info(f"\nNext step: check/edit {rel(srt)}, then:")
    info(f"  python lyricvideo.py preview --project \"{proj.name}\" --time 30")
    info(f"  python lyricvideo.py render --project \"{proj.name}\"")


def _project_srt(proj: Project, explicit: str | None) -> Path:
    if explicit:
        return require_file(explicit, "SRT (--srt)")
    if not proj.srt.is_file():
        raise LyricError(f"The project \"{proj.name}\" has no SRT yet ({rel(proj.srt)}). Run align first.")
    return proj.srt


def project_overrides(proj: Project, args) -> dict:
    """Without --style the values last set in the web UI for this project apply,
    the command line options are applied on top."""
    stored = _load_project_info(proj).get("style_values") if not args.style else None
    base = {k: v for k, v in normalize_choices(stored).items() if k in BUILTIN_STYLE}
    if base:
        info("Using the style values set for this project in the web UI.")
    return {**base, **style_overrides(args)}


def cmd_preview(args, cfg) -> None:
    proj = open_project(args)
    srt = _project_srt(proj, args.srt)
    image = project_source(proj, "image", args.image)
    audio = project_source(proj, "audio", args.audio) if (args.clip or args.audio) else None
    make_preview(image, srt, audio, proj, project_style(proj, args.style, cfg), project_overrides(proj, args), cfg,
                 args.time, args.clip, args.lowres, args.alignment, args.open)


def cmd_render(args, cfg) -> None:
    proj = open_project(args)
    srt = _project_srt(proj, args.srt)
    trim = _load_project_info(proj).get("trim") or {}
    if args.end is not None or args.fade_out is not None:  # remember new values (--end 0 = whole song)
        trim = {"end": args.end if args.end is not None else trim.get("end"),
                "fade": args.fade_out if args.fade_out is not None else trim.get("fade", 2.0)}
        data = _load_project_info(proj)
        data["trim"] = trim
        save_project_info(proj, data)
    make_render(project_source(proj, "image", args.image), project_source(proj, "audio", args.audio), srt, proj,
                project_style(proj, args.style, cfg), project_overrides(proj, args), cfg, args.alignment,
                trim_end=trim.get("end") or None, fade_out=float(trim.get("fade") or 0))


def cmd_web(args, cfg) -> None:
    if importlib.util.find_spec("flask") is None:
        info("The web UI needs the package Flask (approx. 3 MB, installed into venv/ only).")
        if not ask_yes_no("Install it now?", True):
            raise LyricError("Cancelled.")
        if subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "flask"]).returncode != 0:
            raise LyricError("Flask could not be installed (see messages above).")
        importlib.invalidate_caches()
    sys.path.insert(0, str(ROOT))
    import webui
    webui.run(cfg, port=args.port, open_browser=args.open)


def cmd_projects(args, cfg) -> None:
    projects = list_projects()
    if not projects:
        info("No projects in output/ yet.")
        return
    info("Projects in output/ (most recently edited first):")
    for d in projects:
        done = "  (video done)" if (d / f"{d.name}.mp4").exists() else ""
        info(f"  {d.name}{done}")


def cmd_styles(args, cfg) -> None:
    styles = load_styles()
    info("Available styles in styles.json:")
    for n, s in styles.items():
        mark = " (default)" if n == cfg["default_style"] else ""
        info(f"  {n}{mark}: " + ", ".join(f"{k}={v}" for k, v in s.items() if not k.startswith("_")))


def _choose_style(current: str) -> str:
    styles = list(load_styles())
    info(f"Available styles: {', '.join(styles)}")
    while True:
        ans = ask_input(f"Choose a style (Enter = keep \"{current}\"): ", current)
        if ans in styles:
            return ans
        info(f"The style \"{ans}\" does not exist.")


def _ask_seconds(prompt: str, default: float) -> float:
    while True:
        ans = ask_input(f"{prompt} (Enter = {fmt_num(default, 1)}): ", "")
        if not ans:
            return default
        try:
            return parse_time_arg(ans)
        except argparse.ArgumentTypeError as e:
            info(str(e))


def cmd_all(args, cfg) -> None:
    files = _input_files(args, ("audio", "image", "text"))
    proj = create_project(args.project, files["audio"].stem)
    files = import_sources(proj, files)
    audio, image, text = files["audio"], files["image"], files["text"]
    srt, _ = do_align(audio, text, proj, args.backend or cfg["default_backend"],
                      args.language or cfg["default_language"], args.vocals, args.realign, cfg, keep_existing_srt=True)
    info()
    ask_input(f"Check and edit the SRT now, then press Enter.\n  File: {rel(srt)}\n> ")
    style = project_style(proj, args.style, cfg)
    overrides = style_overrides(args)
    if ask_yes_no("Create a preview first?", True):
        last_t = None
        while True:
            info()
            info(f"Current style: \"{style}\"")
            info("  [i] preview image   [c] preview clip with sound   [s] switch style")
            info("  [r] render now      [q] quit")
            choice = ask_input("Choice: ", "r").lower()
            try:
                if choice in ("i", "c"):
                    cues = parse_srt(srt)
                    default_t = last_t if last_t is not None else (cues[0].start + cues[0].end) / 2
                    last_t = _ask_seconds("Time in seconds", default_t)
                    clip = _ask_seconds("Clip length in seconds", 10.0) if choice == "c" else None
                    make_preview(image, srt, audio, proj, style, overrides, cfg, last_t, clip, args.lowres, None, args.open)
                elif choice == "s":
                    style = _choose_style(style)
                elif choice == "r":
                    break
                elif choice == "q":
                    raise LyricError(f"Cancelled. The project is kept: {rel(proj.dir)}")
                else:
                    info("Please enter i, c, s, r or q.")
            except LyricError as e:
                if choice == "q":
                    raise
                print(f"ERROR: {e}", flush=True)
    else:
        style = _choose_style(style)
    make_render(image, audio, srt, proj, style, overrides, cfg, None)
    info(f"\nEverything is in {rel(proj.dir)}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="lyricvideo.py",
        description="Lyric video from a still image, a song and your existing lyrics (forced alignment, ASS subtitles). "
                    "Without a command, 'all' is run.",
    )
    sub = p.add_subparsers(dest="command", metavar="COMMAND")

    def project_arg(sp, new: bool):
        sp.add_argument("--project", "-p", metavar="NAME",
                        help="project name = folder output/NAME/ " + ("(asked otherwise)" if new else "(chosen from a list otherwise)"))

    def input_args(sp, kinds):
        labels = {"audio": "audio file", "image": "image", "text": "lyrics (.txt, one sung line per line)"}
        for k in kinds:
            sp.add_argument(f"--{k}", help=f"{labels[k]} (default: the file in input/, any name)")

    def align_args(sp):
        sp.add_argument("--backend", choices=["elevenlabs", "local"], help="alignment backend (default: elevenlabs)")
        sp.add_argument("--language", metavar="CODE", help="language code, e.g. en, de, es (only for --backend local)")
        sp.add_argument("--vocals", action="store_true", help="separate the vocals with demucs first (alignment only)")
        sp.add_argument("--realign", action="store_true", help="ignore the stored alignment and align again")

    def yes_arg(sp):
        sp.add_argument("-y", "--yes", action="store_true", help="overwrite existing files without asking")

    def override_args(sp):
        sp.add_argument("--image", help="use a different image than the project's")
        sp.add_argument("--audio", help="use a different audio file than the project's")
        sp.add_argument("--srt", help="use a different SRT than the project's")
        sp.add_argument("--alignment", help="use a different alignment.json than the project's")

    sp = sub.add_parser("all", help="everything: align, edit, preview, render (default)")
    project_arg(sp, True)
    input_args(sp, ("audio", "image", "text"))
    align_args(sp)
    sp.add_argument("--lowres", action="store_true", help="preview at half resolution")
    sp.add_argument("--open", action=argparse.BooleanOptionalAction, default=None, help="open the preview afterwards")
    add_style_args(sp)
    yes_arg(sp)
    sp.set_defaults(func=cmd_all)

    sp = sub.add_parser("align", help="audio + lyrics -> SRT")
    project_arg(sp, True)
    input_args(sp, ("audio", "text"))
    align_args(sp)
    yes_arg(sp)
    sp.set_defaults(func=cmd_align)

    sp = sub.add_parser("preview", help="quick preview (PNG or clip)")
    project_arg(sp, False)
    sp.add_argument("--time", type=parse_time_arg, help="time in seconds or m:ss (default: first line)")
    sp.add_argument("--clip", type=float, help="instead of a still, a clip of this length (seconds) with sound")
    sp.add_argument("--lowres", action="store_true", help="half resolution (even faster)")
    sp.add_argument("--open", action=argparse.BooleanOptionalAction, default=None, help="open the preview afterwards")
    override_args(sp)
    add_style_args(sp)
    sp.set_defaults(func=cmd_preview)

    sp = sub.add_parser("render", help="project -> MP4")
    project_arg(sp, False)
    sp.add_argument("--end", type=parse_time_arg, help="end the video here, e.g. 5:40 (0 = whole song); remembered")
    sp.add_argument("--fade-out", type=float, help="fade out over this many seconds at the end (default 2); remembered")
    override_args(sp)
    add_style_args(sp)
    yes_arg(sp)
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser("web", help="start the web UI in the browser")
    sp.add_argument("--port", type=int, help="port (default: web.port from config.json, otherwise 8765)")
    sp.add_argument("--open", action=argparse.BooleanOptionalAction, default=True, help="open the browser automatically")
    sp.set_defaults(func=cmd_web)

    sp = sub.add_parser("projects", help="list projects")
    sp.set_defaults(func=cmd_projects)

    sp = sub.add_parser("styles", help="list style presets")
    sp.set_defaults(func=cmd_styles)
    return p


def _ensure_venv() -> None:
    """Restarts the script with the Python from venv/ if it was started with the system Python."""
    if sys.prefix != sys.base_prefix or os.environ.get("LYRICVIDEO_NO_VENV"):
        return
    if VENV_PY.exists():
        try:
            sys.exit(subprocess.call([str(VENV_PY), str(Path(__file__).resolve()), *sys.argv[1:]]))
        except KeyboardInterrupt:
            sys.exit(130)
    else:
        warn("No venv found (folder venv/). See README for setup.")


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        _ensure_venv()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raw = list(argv if argv is not None else sys.argv[1:])
    if not raw or (raw[0].startswith("-") and raw[0] not in ("-h", "--help")):
        raw = ["all", *raw]  # no command: everything in one go
    args = build_parser().parse_args(raw)
    global ASSUME_YES
    ASSUME_YES = bool(getattr(args, "yes", False))
    try:
        cfg = load_config()
        if args.command not in ("styles", "web"):
            check_ffmpeg()
        args.func(args, cfg)
    except LyricError as e:
        print(f"\nERROR: {e}", file=sys.stderr, flush=True)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled (Ctrl+C).", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
