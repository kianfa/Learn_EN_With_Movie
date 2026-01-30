# phraseclipper_core.py
from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np
from moviepy.editor import (
    VideoFileClip,
    concatenate_videoclips,
    CompositeVideoClip,
    ImageClip,
)
from PIL import Image, ImageDraw, ImageFont
import textwrap


# -----------------------------
# Data models
# -----------------------------
@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Match:
    subtitle_file: Path
    start: float
    end: float
    text: str
    video_file: Optional[Path] = None


@dataclass(frozen=True)
class Settings:
    pad_before: float = 0.5
    pad_after: float = 1.5

    resize_to: Optional[tuple[int, int]] = (1280, 720)
    add_black_between: bool = True
    black_clip_path: Optional[Path] = None

    burn_subtitles: bool = True
    subtitle_font: str = "Times New Roman"
    subtitle_fontsize: int = 36

    case_insensitive: bool = True


# -----------------------------
# SRT parsing
# -----------------------------
_TS_RE = re.compile(
    r"(?P<h1>\d{2}):(?P<m1>\d{2}):(?P<s1>\d{2}),(?P<ms1>\d{3})\s*-->\s*"
    r"(?P<h2>\d{2}):(?P<m2>\d{2}):(?P<s2>\d{2}),(?P<ms2>\d{3})"
)


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[Caption]:
    lines = path.read_text(errors="ignore").splitlines()
    captions: list[Caption] = []

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = _TS_RE.search(line)
        if not m:
            i += 1
            continue

        start = _to_seconds(m["h1"], m["m1"], m["s1"], m["ms1"])
        end = _to_seconds(m["h2"], m["m2"], m["s2"], m["ms2"])

        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip() != "":
            if not lines[i].strip().isdigit():
                t = lines[i].strip().replace("<i>", "").replace("</i>", "")
                text_lines.append(t)
            i += 1

        text = " ".join(text_lines).strip()
        if text:
            captions.append(Caption(start=start, end=end, text=text))

        i += 1

    return captions


# -----------------------------
# Phrase searching
# -----------------------------
def find_phrase_matches(subtitle_file: Path, phrase: str, *, case_insensitive: bool) -> list[Match]:
    raw = subtitle_file.read_text(errors="ignore")
    if case_insensitive:
        if phrase.casefold() not in raw.casefold():
            return []
    else:
        if phrase not in raw:
            return []

    caps = parse_srt(subtitle_file)

    if case_insensitive:
        phrase_cmp = phrase.casefold()

        def hit(t: str) -> bool:
            return phrase_cmp in t.casefold()
    else:

        def hit(t: str) -> bool:
            return phrase in t

    out: list[Match] = []
    for c in caps:
        if hit(c.text):
            out.append(Match(subtitle_file=subtitle_file, start=c.start, end=c.end, text=c.text))
    return out


# -----------------------------
# Video location (ROBUST)
# -----------------------------
_SXXEYY_RE = re.compile(r"(S\d{2}).*?(E\d{2})", re.IGNORECASE)


def _extract_sxxeyy(token: str) -> Optional[str]:
    m = _SXXEYY_RE.search(token)
    if not m:
        return None
    return (m.group(1) + m.group(2)).upper()


def _tokens_from_release_name(stem: str) -> list[str]:
    parts = re.split(r"[.\s_\-\[\]\(\)]+", stem)
    return [p for p in parts if p]


def _best_show_token_from_subtitle(subtitle_file: Path) -> Optional[str]:
    # for "ozark.S02E10" -> "ozark"
    # for "the.big.bang.theory.S02E10" -> "the" (too weak) -> prefer first 2+ chars token
    parts = _tokens_from_release_name(subtitle_file.stem)
    junk = {"1080p", "720p", "480p", "2160p", "4k", "web", "webrip", "webdl", "bluray", "brrip", "hdrip", "dvdrip"}
    for t in parts:
        tl = t.casefold()
        if tl in junk:
            continue
        if _SXXEYY_RE.search(t):
            continue
        if tl.isdigit():
            continue
        if len(tl) >= 3:
            return tl
    return None


def locate_video_for_subtitle(
    subtitle_file: Path,
    video_roots: Sequence[Path],
    video_exts: Sequence[str] = (".mkv", ".mp4", ".m4v", ".avi"),
) -> Optional[Path]:
    exts = {e.lower() for e in video_exts}

    # 0) Same folder exact match
    for ext in exts:
        candidate = subtitle_file.with_suffix(ext)
        if candidate.exists():
            return candidate

    # 0b) Same folder fuzzy stem match
    sub_stem_cf = subtitle_file.stem.casefold()
    try:
        for p in subtitle_file.parent.iterdir():
            if p.is_file() and p.suffix.lower() in exts:
                v_cf = p.stem.casefold()
                if sub_stem_cf in v_cf or v_cf in sub_stem_cf:
                    return p
    except Exception:
        pass

    # 1) Series token SxxEyy - REQUIRE show token too (fixes Ozark/Dexter bug)
    sxxeyy = _extract_sxxeyy(subtitle_file.name)
    show_tok = _best_show_token_from_subtitle(subtitle_file)
    if sxxeyy:
        for root in video_roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if not (p.is_file() and p.suffix.lower() in exts):
                    continue
                name_cf = p.name.casefold()
                path_cf = str(p).casefold()
                if sxxeyy.casefold() in name_cf:
                    if show_tok and show_tok not in name_cf and show_tok not in path_cf:
                        continue
                    return p
        return None

    # 2) Movie/release name matching
    parts = _tokens_from_release_name(subtitle_file.stem)
    year = next((x for x in parts if re.fullmatch(r"(19|20)\d{2}", x)), None)

    junk = {
        "1080p", "720p", "480p", "2160p", "4k",
        "x264", "x265", "h264", "h265", "hevc",
        "web", "webrip", "webdl", "web-dl", "bluray", "brrip", "hdrip", "dvdrip",
        "aac", "dts", "truehd", "atmos", "mp3",
        "proper", "repack", "extended", "remux", "subs", "subbed", "dubbed",
        "galaxyrg", "film2media", "yts", "yify",
        "hdr", "sdr", "dv", "dolby",
        "dl", "rip"
    }

    cleaned: list[str] = []
    for t in parts:
        tl = t.casefold()
        if tl in junk:
            continue
        if len(t) <= 1 and not t.isdigit():
            continue
        cleaned.append(t)

    if year and year in cleaned:
        idx = cleaned.index(year)
        title_tokens = cleaned[:idx]
    else:
        title_tokens = cleaned[:4]

    title_tokens_cf = [t.casefold() for t in title_tokens if len(t) >= 2 or t.isdigit()]
    if not title_tokens_cf:
        return None

    required = title_tokens_cf[:3]
    required_with_year = required + ([year.casefold()] if year else [])

    def match_required(name_cf: str, req: list[str]) -> bool:
        return all(tok in name_cf for tok in req)

    # Pass A: title + year
    if required_with_year:
        for root in video_roots:
            if not root.exists():
                continue
            for p in root.rglob("*"):
                if p.is_file() and p.suffix.lower() in exts:
                    n = p.name.casefold()
                    if match_required(n, required_with_year):
                        return p

    # Pass B: title only
    for root in video_roots:
        if not root.exists():
            continue
        for p in root.rglob("*"):
            if p.is_file() and p.suffix.lower() in exts:
                n = p.name.casefold()
                if match_required(n, required):
                    return p

    return None


# -----------------------------
# Subtitles without ImageMagick (Pillow)
# -----------------------------
def pil_subtitle_clip(
    text: str,
    video_w: int,
    video_h: int,
    duration: float,
    font_size: int = 36,
    font_path: Optional[str] = None,
    color: tuple[int, int, int] = (255, 255, 255),
    stroke_color: tuple[int, int, int] = (0, 0, 0),
    stroke_width: int = 2,
    margin_bottom: int = 40,
) -> ImageClip:
    box_w = int(video_w * 0.90)
    box_h = int(video_h * 0.25)

    wrap_width = max(20, int(box_w / (font_size * 0.55)))
    lines = textwrap.fill(text.strip(), width=wrap_width)

    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            font = None

    if font is None:
        candidates = [
            r"C:\Windows\Fonts\arial.ttf",
            r"C:\Windows\Fonts\arialbd.ttf",
            r"C:\Windows\Fonts\times.ttf",
            r"C:\Windows\Fonts\timesbd.ttf",
        ]
        for fp in candidates:
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                font = None

    if font is None:
        font = ImageFont.load_default()

    img = Image.new("RGBA", (box_w, box_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    bbox = draw.multiline_textbbox((0, 0), lines, font=font, align="center", stroke_width=stroke_width)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]

    x = (box_w - text_w) // 2
    y = (box_h - text_h) // 2

    draw.multiline_text(
        (x, y),
        lines,
        font=font,
        fill=color,
        align="center",
        stroke_fill=stroke_color,
        stroke_width=stroke_width,
    )

    arr = np.array(img)  # (H, W, 4)
    clip = ImageClip(arr).set_duration(duration)
    return clip.set_pos(("center", video_h - box_h - margin_bottom))


# -----------------------------
# Clip building / exporting (supports per-item padding overrides)
# -----------------------------
def build_clip(
    video_path: Path,
    match: Match,
    settings: Settings,
    manual_offset_start: float = 0.0,
    manual_offset_end: float = 0.0,
    pad_before_override: Optional[float] = None,
    pad_after_override: Optional[float] = None,
) -> VideoFileClip:
    clip = VideoFileClip(str(video_path), fps_source="fps")

    pad_before = settings.pad_before if pad_before_override is None else float(pad_before_override)
    pad_after = settings.pad_after if pad_after_override is None else float(pad_after_override)

    # note: negative paddings are allowed (start later / end earlier)
    start = max(0.0, match.start - pad_before + manual_offset_start)
    end = min(clip.duration, match.end + pad_after + manual_offset_end)
    if end <= start:
        raise ValueError("Computed clip end <= start. Adjust padding/offsets.")

    clip = clip.subclip(start, end)

    if settings.resize_to:
        clip = clip.resize(settings.resize_to)

    if not settings.burn_subtitles:
        return clip

    w, h = clip.size
    sub = pil_subtitle_clip(
        text=match.text,
        video_w=w,
        video_h=h,
        duration=max(0.3, clip.duration - 0.1),
        font_size=settings.subtitle_fontsize,
    )

    return CompositeVideoClip([clip, sub])


def export_compilation(
    matches: Sequence[Match],
    video_roots: Sequence[Path],
    output_path: Path,
    settings: Settings,
    offsets: Optional[dict[int, tuple[float, float]]] = None,
    pad_overrides: Optional[dict[int, tuple[Optional[float], Optional[float]]]] = None,
    on_progress: Optional[callable] = None,
    moviepy_logger=None,
) -> None:
    offsets = offsets or {}
    pad_overrides = pad_overrides or {}

    clips = []
    black_clip = None

    if settings.add_black_between:
        if settings.black_clip_path and settings.black_clip_path.exists():
            black_clip = VideoFileClip(str(settings.black_clip_path))
            if settings.resize_to:
                black_clip = black_clip.resize(settings.resize_to)
        else:
            from moviepy.editor import ColorClip
            size = settings.resize_to or (1280, 720)
            black_clip = ColorClip(size=size, color=(0, 0, 0)).set_duration(0.5)

    total = len(matches)
    for idx, m in enumerate(matches):
        if on_progress:
            on_progress(idx + 1, total, f"Locating video for: {m.subtitle_file.name}")

        vp = m.video_file or locate_video_for_subtitle(m.subtitle_file, video_roots)
        if not vp:
            raise FileNotFoundError(f"Could not locate video for subtitle: {m.subtitle_file.name}")

        start_off, end_off = offsets.get(idx, (0.0, 0.0))
        pad_b, pad_a = pad_overrides.get(idx, (None, None))

        if on_progress:
            on_progress(idx + 1, total, f"Building clips: {vp.name}")

        settings_no_sub = Settings(**{**settings.__dict__, "burn_subtitles": False})
        clip_plain = build_clip(vp, m, settings_no_sub, start_off, end_off, pad_b, pad_a).crossfadeout(1.0)
        clips.append(clip_plain)

        if black_clip is not None:
            clips.append(black_clip.subclip(0, min(0.5, black_clip.duration)))

        clip_sub = build_clip(vp, m, settings, start_off, end_off, pad_b, pad_a).crossfadein(1.0)
        clips.append(clip_sub)

    final = concatenate_videoclips(clips, method="compose")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if on_progress:
        on_progress(total, total, f"Writing: {output_path.name}")

    final.write_videofile(
        str(output_path),
        threads=4,
        logger=moviepy_logger,
    )

    try:
        final.close()
        for c in clips:
            c.close()
        if black_clip is not None:
            black_clip.close()
    except Exception:
        pass


def _aggressive_close(clip):
    try:
        clip.close()
    except Exception:
        pass
    try:
        if getattr(clip, "reader", None):
            clip.reader.close()
    except Exception:
        pass
    try:
        if getattr(clip, "audio", None) and getattr(clip.audio, "reader", None):
            clip.audio.reader.close_proc()
    except Exception:
        pass


def make_temp_preview(
    match: Match,
    video_roots: Sequence[Path],
    settings: Settings,
    pad_before_override: Optional[float] = None,
    pad_after_override: Optional[float] = None,
    moviepy_logger=None,
) -> Path:
    vp = match.video_file or locate_video_for_subtitle(match.subtitle_file, video_roots)
    if not vp:
        raise FileNotFoundError(f"Could not locate video for subtitle: {match.subtitle_file.name}")

    clip = build_clip(
        vp,
        match,
        settings,
        manual_offset_start=0.0,
        manual_offset_end=0.0,
        pad_before_override=pad_before_override,
        pad_after_override=pad_after_override,
    )

    fd, tmpname = tempfile.mkstemp(prefix="phraseclip_preview_", suffix=".mp4")
    try:
        Path(tmpname).unlink(missing_ok=True)
    except Exception:
        pass

    tmp = Path(tmpname)
    clip.write_videofile(
        str(tmp),
        threads=2,
        logger=moviepy_logger,
    )
    _aggressive_close(clip)
    return tmp
