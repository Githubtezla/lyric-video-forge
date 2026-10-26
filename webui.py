# -*- coding: utf-8 -*-
"""
Web UI for lyricvideo.py.

Start:  python lyricvideo.py web      (or double-click LyricVideo-Web.bat on Windows)
Runs on this computer only (http://127.0.0.1:8765) and uses the same functions as the terminal.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import traceback
import urllib.parse
import uuid
import webbrowser
from pathlib import Path

from flask import Flask, Response, jsonify, request, send_from_directory

import lyricvideo as lv

WEB_DIR = lv.ROOT / "web"
app = Flask(__name__, static_folder=None)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 ** 3

# --------------------------------------------------------------------------- #
# Collect messages from the functions and prevent prompts in the terminal
# --------------------------------------------------------------------------- #

_tl = threading.local()
_orig_info = lv.info


def _info(msg: str = "") -> None:
    _orig_info(msg)
    log = getattr(_tl, "log", None)
    if log is not None:
        log.append(str(msg))


def _warn(msg: str) -> None:
    _info(f"WARNING: {msg}")


def _no_terminal_input(prompt: str = "", default: str = "") -> str:
    raise lv.LyricError("This action would need input in the terminal: " + prompt.strip())


def _install_hooks() -> None:
    lv.info = _info
    lv.warn = _warn
    lv.ask_input = _no_terminal_input
    lv.ask_yes_no = lambda question, default=False: default  # e.g. reuse a stored alignment = yes
    lv.ASSUME_YES = True  # overwriting is confirmed in the browser


class _Capture:
    """Collects the messages of the current thread."""

    def __enter__(self):
        self.log: list[str] = []
        self._old = getattr(_tl, "log", None)
        _tl.log = self.log
        return self

    def __exit__(self, *exc):
        _tl.log = self._old

    @property
    def warnings(self) -> list[str]:
        return [l.replace("WARNING: ", "", 1).strip() for l in self.log if l.startswith("WARNING")]


_preview_lock = threading.Lock()

# --------------------------------------------------------------------------- #
# Background jobs (align, render)
# --------------------------------------------------------------------------- #

RUNNING, DONE, FAILED = "running", "done", "error"


class Job:
    def __init__(self, kind: str, project: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.project = project
        self.status = RUNNING
        self.log: list[str] = []
        self.progress: float | None = None
        self.error: str | None = None
        self.result: dict | None = None
        self.started = time.time()

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "project": self.project, "status": self.status,
                "log": self.log, "progress": self.progress, "error": self.error, "result": self.result,
                "seconds": round(time.time() - self.started, 1)}


_jobs: dict[str, Job] = {}
_jobs_lock = threading.Lock()


def _start_job(kind: str, project: str, fn) -> Job:
    with _jobs_lock:
        running = [j for j in _jobs.values() if j.status == RUNNING]
        if running:
            raise lv.LyricError(f"A job is already running ({running[0].kind}, project \"{running[0].project}\"). "
                                "Please wait until it has finished.")
        job = Job(kind, project)
        _jobs[job.id] = job

    def run():
        _tl.log = job.log
        try:
            job.result = fn(job) or {}
            job.status = DONE
        except lv.LyricError as e:
            job.error = str(e)
            job.status = FAILED
        except Exception as e:  # unexpected: full details in the terminal, short in the browser
            traceback.print_exc()
            job.error = f"Unexpected error: {type(e).__name__}: {e}"
            job.status = FAILED
        finally:
            _tl.log = None

    threading.Thread(target=run, daemon=True).start()
    return job


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@app.errorhandler(lv.LyricError)
def _lyric_error(e):
    return jsonify(error=str(e)), 400


def _project(name: str, create: bool = False) -> lv.Project:
    clean = lv.safe_project_name(name)
    if not clean:
        raise lv.LyricError("Please enter a valid project name (no characters like \\ / : * ? \" < > |).")
    proj = lv.Project(clean)
    if create:
        proj.dir.mkdir(parents=True, exist_ok=True)
    elif not proj.dir.is_dir():
        raise lv.LyricError(f"The project \"{name}\" does not exist.")
    return proj


def _save_info(proj: lv.Project, **values) -> None:
    data = lv._load_project_info(proj)
    data.update(values)
    data.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%S"))
    lv.save_project_info(proj, data)


def _source(proj: lv.Project, kind: str) -> Path | None:
    stored = lv._load_project_info(proj).get(kind)
    if stored and (proj.dir / stored).is_file():
        return proj.dir / stored
    files = lv.files_of_kind(proj.src_dir, kind)
    return files[0] if len(files) == 1 else None


def _require_source(proj: lv.Project, kind: str) -> Path:
    path = _source(proj, kind)
    if path is None:
        raise lv.LyricError(f"The project is still missing: {lv.KIND_LABEL[kind]}. Please upload it first.")
    return path


def _file_url(proj: lv.Project, path: Path) -> str:
    relpath = path.relative_to(proj.dir).as_posix()
    return (f"/files/{urllib.parse.quote(proj.name)}/{urllib.parse.quote(relpath)}"
            f"?v={int(path.stat().st_mtime)}")


def _safe_filename(name: str) -> str:
    name = Path(name or "").name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip().strip(".")
    return name or "file"


_hash_cache: dict[tuple, str] = {}


def _file_hash(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_mtime, st.st_size)
    if key not in _hash_cache:
        _hash_cache[key] = lv.sha256_file(path)
    return _hash_cache[key]


def _align_status(proj: lv.Project, audio: Path | None, text: Path | None) -> str:
    if not proj.alignment.is_file():
        return "missing"
    if audio is None or text is None:
        return "unknown"
    try:
        meta = json.loads(lv.read_text_file(proj.alignment)).get("_lyricvideo", {})
        lines = [l.text for l in lv.parse_lyrics(text)]
    except (json.JSONDecodeError, OSError, lv.LyricError):
        return "unknown"
    current = (meta.get("audio_sha256") == _file_hash(audio)
               and meta.get("text_key") == lv.sha256_text(lv.norm("\n".join(lines))))
    return "current" if current else "outdated"


def _style_values(style: dict) -> dict:
    out = {k: v for k, v in style.items() if not k.startswith("_") and k != "background"}
    out["dim_percent"] = style["background"]["dim_percent"]
    return out


def _overrides(values: dict | None) -> dict:
    """Values from the browser -> style fields (dim_percent belongs to background)."""
    out = {}
    for k, v in (values or {}).items():
        if k == "dim_percent":
            out["background"] = {"dim_percent": v}
        elif k in lv.BUILTIN_STYLE and k != "background":
            out[k] = v
    return out


def _all_styles() -> dict:
    result = {}
    for name in lv.load_styles():
        style, _ = lv.resolve_style(name, {})
        result[name] = _style_values(style)
    return result


_STYLE_WORDS = {"bold", "italic", "oblique", "regular", "light", "semibold", "semilight", "demibold", "medium",
                "thin", "extralight", "ultralight", "extrabold", "heavy", "book", "normal"}


def _font_families() -> list[str]:
    names: set[str] = set()
    try:
        import winreg
        for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            try:
                key = winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")
            except OSError:
                continue
            with key:
                i = 0
                while True:
                    try:
                        value = winreg.EnumValue(key, i)[0]
                    except OSError:
                        break
                    i += 1
                    base = re.sub(r"\s*\([^)]*\)\s*$", "", value)
                    for part in base.split("&"):
                        words = part.split()
                        while len(words) > 1 and words[-1].casefold() in _STYLE_WORDS:
                            words.pop()
                        if words:
                            names.add(" ".join(words))
    except ImportError:
        pass
    try:
        from fontTools.ttLib import TTCollection, TTFont
        for p in lv.FONTS_DIR.glob("*"):
            if p.suffix.lower() not in (".ttf", ".otf", ".ttc", ".otc"):
                continue
            try:
                fonts = TTCollection(str(p)).fonts if p.suffix.lower() in (".ttc", ".otc") else [TTFont(str(p), lazy=True)]
                for f in fonts:
                    fam = f["name"].getDebugName(16) or f["name"].getDebugName(1)
                    if fam:
                        names.add(fam)
            except Exception:
                pass
    except ImportError:
        pass
    return sorted(names, key=str.casefold)


def _sample_srt(proj: lv.Project, text: Path | None) -> Path:
    """For previews before aligning: the first lines of the lyrics, one every 4 seconds."""
    lines = [l.text for l in lv.parse_lyrics(text)] if text else []
    lines = lines[:30] or ["Your lyrics will appear here"]
    cues = [lv.Cue(i * 4.0, i * 4.0 + 3.6, t, i + 1) for i, t in enumerate(lines)]
    path = proj.dir / "_sample.srt"
    lv.write_srt(path, cues)
    return path


# --------------------------------------------------------------------------- #
# Pages and files
# --------------------------------------------------------------------------- #


@app.get("/")
def index():
    resp = send_from_directory(WEB_DIR, "index.html")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/files/<name>/<path:relpath>")
def project_file(name, relpath):
    proj = _project(name)
    resp = send_from_directory(proj.dir, relpath, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #


@app.get("/api/state")
def api_state():
    cfg = lv.load_config()
    projects = []
    if lv.OUTPUT_DIR.is_dir():
        dirs = [d for d in lv.OUTPUT_DIR.iterdir() if d.is_dir()]
        dirs.sort(key=lambda d: max([d.stat().st_mtime] + [f.stat().st_mtime for f in d.iterdir()]), reverse=True)
        projects = [d.name for d in dirs]
    try:
        lv.load_api_key()
        has_key = True
    except lv.LyricError:
        has_key = False
    return jsonify(projects=projects, styles=_all_styles(), default_style=cfg["default_style"],
                   fonts=_font_families(), has_key=has_key, width=cfg["width"], height=cfg["height"],
                   input_files={k: [p.name for p in lv.files_of_kind(lv.INPUT_DIR, k)] for k in lv.KIND_EXT})


@app.post("/api/project")
def api_project_create():
    proj = _project((request.get_json(silent=True) or {}).get("name", ""), create=True)
    return jsonify(name=proj.name)


@app.get("/api/project/<name>")
def api_project(name):
    proj = _project(name)
    audio, image, text = _source(proj, "audio"), _source(proj, "image"), _source(proj, "text")
    info = lv._load_project_info(proj)
    files = {k: ({"name": p.name, "url": _file_url(proj, p)} if p else None)
             for k, p in (("audio", audio), ("image", image), ("text", text))}
    srt_text, srt_error, cues = None, None, []
    if proj.srt.is_file():
        srt_text = lv.read_text_file(proj.srt)
        try:
            cues = [{"n": c.number, "start": c.start, "end": c.end, "text": c.text} for c in lv.parse_srt(proj.srt)]
        except lv.LyricError as e:
            srt_error = str(e)
    duration = None
    if audio:
        try:
            duration = lv.media_duration(audio)
        except lv.LyricError:
            pass
    video = proj.file(".mp4")
    stored_values = info.get("style_values")
    return jsonify(
        name=proj.name, files=files,
        text=lv.read_text_file(text) if text else "",
        srt=srt_text, srt_error=srt_error, cues=cues, duration=duration,
        align_status=_align_status(proj, audio, text),
        style=info.get("style"), style_values=lv.normalize_choices(stored_values) if stored_values else None,
        trim=info.get("trim") or {"end": None, "fade": 2.0},
        video={"url": _file_url(proj, video), "mb": round(video.stat().st_size / 1024 ** 2, 1)} if video.is_file() else None,
        folder=str(proj.dir),
    )


@app.post("/api/project/<name>/upload")
def api_upload(name):
    proj = _project(name)
    kind = request.form.get("kind", "")
    f = request.files.get("file")
    if kind not in lv.KIND_EXT or f is None:
        raise lv.LyricError("Invalid upload.")
    filename = _safe_filename(f.filename)
    if Path(filename).suffix.lower() not in lv.KIND_EXT[kind]:
        raise lv.LyricError(f"{lv.KIND_LABEL[kind]}: allowed are {', '.join(sorted(lv.KIND_EXT[kind]))} "
                            f"(uploaded: {filename}).")
    proj.src_dir.mkdir(parents=True, exist_ok=True)
    dest = proj.src_dir / filename
    f.save(dest)
    _save_info(proj, **{kind: dest.relative_to(proj.dir).as_posix()})
    return jsonify(ok=True, name=filename)


@app.post("/api/project/<name>/import-input")
def api_import_input(name):
    proj = _project(name)
    found, missing = {}, []
    for kind in ("audio", "image", "text"):
        files = lv.files_of_kind(lv.INPUT_DIR, kind)
        if len(files) == 1:
            found[kind] = files[0]
        elif len(files) > 1:
            missing.append(f"{lv.KIND_LABEL[kind]} (several in input/ – please upload one)")
        else:
            missing.append(f"{lv.KIND_LABEL[kind]} (not in input/)")
    if not found:
        raise lv.LyricError("No matching files were found in input/.")
    with _Capture():
        lv.import_sources(proj, found)
    return jsonify(imported=[p.name for p in found.values()], missing=missing)


@app.post("/api/project/<name>/text")
def api_text(name):
    proj = _project(name)
    text = (request.get_json(silent=True) or {}).get("text", "")
    path = _source(proj, "text")
    if path is None:
        proj.src_dir.mkdir(parents=True, exist_ok=True)
        path = proj.src_dir / "lyrics.txt"
    path.write_text(text, encoding="utf-8")
    _save_info(proj, text=path.relative_to(proj.dir).as_posix())
    lines = lv.parse_lyrics(path)
    return jsonify(ok=True, lines=len(lines))


@app.post("/api/project/<name>/srt")
def api_srt(name):
    proj = _project(name)
    text = (request.get_json(silent=True) or {}).get("srt", "")
    check = proj.dir / "_check.srt"
    check.write_text(text, encoding="utf-8")
    try:
        cues = lv.parse_srt(check)
    except lv.LyricError as e:
        check.unlink(missing_ok=True)
        raise lv.LyricError(str(e).replace(lv.rel(check), "SRT"))
    os.replace(check, proj.srt)
    return jsonify(ok=True, cues=len(cues))


@app.post("/api/project/<name>/align")
def api_align(name):
    proj = _project(name)
    body = request.get_json(silent=True) or {}
    audio, text = _require_source(proj, "audio"), _require_source(proj, "text")
    cfg = lv.load_config()

    def work(job):
        lv.do_align(audio, text, proj, "elevenlabs", cfg["default_language"], False,
                    bool(body.get("realign")), cfg)
        return {}

    return jsonify(job=_start_job("align", proj.name, work).as_dict())


@app.post("/api/project/<name>/preview")
def api_preview(name):
    proj = _project(name)
    body = request.get_json(silent=True) or {}
    cfg = lv.load_config()
    image = _require_source(proj, "image")
    sample = not proj.srt.is_file()
    preset = body.get("preset") or cfg["default_style"]
    values = body.get("style") or {}
    at = float(body.get("time") or 0)
    with _preview_lock, _Capture() as cap:
        srt = _sample_srt(proj, _source(proj, "text")) if sample else proj.srt
        out = lv.make_preview(image, srt, None, proj, preset, _overrides(values), cfg, at, None,
                              not body.get("full"), None, False)
        data = out.read_bytes()
        cues = lv.parse_srt(srt)
    if body.get("remember"):  # only if the style was actually changed in the browser
        _save_info(proj, style=preset, style_values=values)
    active = [c.text for c in cues if c.start <= at < c.end]
    warnings = cap.warnings
    if sample:
        warnings = [w for w in warnings if "alignment.json" not in w]
    info = {"warnings": warnings, "active": active, "sample": sample}
    resp = Response(data, mimetype="image/png")
    resp.headers["X-Info"] = urllib.parse.quote(json.dumps(info, ensure_ascii=False))
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.post("/api/project/<name>/clip")
def api_clip(name):
    proj = _project(name)
    body = request.get_json(silent=True) or {}
    cfg = lv.load_config()
    image, audio = _require_source(proj, "image"), _require_source(proj, "audio")
    if not proj.srt.is_file():
        raise lv.LyricError("Please align the lyrics first to make a clip.")
    preset = body.get("preset") or cfg["default_style"]
    with _preview_lock, _Capture() as cap:
        out = lv.make_preview(image, proj.srt, audio, proj, preset, _overrides(body.get("style")), cfg,
                              float(body.get("time") or 0), float(body.get("length") or 10),
                              not body.get("full"), None, False)
    return jsonify(url=_file_url(proj, out), warnings=cap.warnings)


@app.post("/api/project/<name>/render")
def api_render(name):
    proj = _project(name)
    body = request.get_json(silent=True) or {}
    cfg = lv.load_config()
    image, audio = _require_source(proj, "image"), _require_source(proj, "audio")
    if not proj.srt.is_file():
        raise lv.LyricError("Please align the lyrics first (there is no SRT yet).")
    preset = body.get("preset") or cfg["default_style"]
    values = body.get("style") or {}
    try:
        trim_end = float(body["trim_end"]) if body.get("trim_end") not in (None, "") else None
        fade_out = float(body.get("fade_out") or 0)
    except (TypeError, ValueError):
        raise lv.LyricError("Invalid trim value (end or fade-out).")
    if trim_end is not None and trim_end <= 0:
        trim_end = None
    _save_info(proj, style=preset, style_values=values, trim={"end": trim_end, "fade": fade_out})

    def work(job):
        def progress(share):
            job.progress = round(share, 4)
        out = lv.make_render(image, audio, proj.srt, proj, preset, _overrides(values), cfg, None, progress=progress,
                             trim_end=trim_end, fade_out=fade_out)
        job.progress = 1.0
        return {"url": _file_url(proj, out), "path": str(out),
                "mb": round(out.stat().st_size / 1024 ** 2, 1)}

    return jsonify(job=_start_job("render", proj.name, work).as_dict())


@app.get("/api/job/<job_id>")
def api_job(job_id):
    job = _jobs.get(job_id)
    if job is None:
        raise lv.LyricError("Job not found.")
    return jsonify(job=job.as_dict())


@app.get("/api/jobs/running")
def api_jobs_running():
    running = [j.as_dict() for j in _jobs.values() if j.status == RUNNING]
    return jsonify(jobs=running)


@app.post("/api/styles/save")
def api_style_save():
    body = request.get_json(silent=True) or {}
    name = re.sub(r"[^\w \-]", "", str(body.get("name", ""))).strip()
    if not name or name.startswith("_"):
        raise lv.LyricError("Please enter a name for the style (letters, digits, spaces, - and _).")
    overrides = _overrides(body.get("style"))
    lv.validate_style({**lv.BUILTIN_STYLE, **overrides, "background": overrides.get("background", {"dim_percent": 0})},
                      name)  # checks the values before saving
    data = lv.load_json_file(lv.STYLES_FILE, "styles.json") if lv.STYLES_FILE.exists() else {}
    data[name] = overrides
    lv.STYLES_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return jsonify(ok=True, name=name, styles=_all_styles())


@app.post("/api/open-folder/<name>")
def api_open_folder(name):
    proj = _project(name)
    if hasattr(os, "startfile"):
        os.startfile(str(proj.dir))
    return jsonify(ok=True)


# --------------------------------------------------------------------------- #
# Start
# --------------------------------------------------------------------------- #


def _disable_quick_edit() -> None:
    """On Windows a click into the console window (QuickEdit mode) pauses the program –
    the page then stops responding. QuickEdit is switched off for this window."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, (mode.value & ~0x0040) | 0x0080)  # QUICK_EDIT off, EXTENDED_FLAGS on
    except Exception:
        pass


def run(cfg: dict, port: int | None = None, open_browser: bool = True) -> None:
    _install_hooks()
    _disable_quick_edit()
    lv.check_ffmpeg()
    port = port or int(cfg.get("web", {}).get("port", 8765))
    url = f"http://127.0.0.1:{port}"
    logging.getLogger("werkzeug").setLevel(logging.WARNING)
    print(f"\nWeb UI running: {url}\nKeep this window open. Stop with Ctrl+C.\n", flush=True)
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    try:
        app.run(host="127.0.0.1", port=port, threaded=True, debug=False, use_reloader=False)
    except OSError as e:
        raise lv.LyricError(f"The server could not start on port {port} ({e}). "
                            f"Try another port: python lyricvideo.py web --port {port + 1}")
