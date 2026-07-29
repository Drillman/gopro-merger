"""Decouverte des rushs GoPro, extraction de la telemetrie, concatenation, encodage."""

import json
import logging
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("trackday.video")

CLIP_RE = re.compile(r"^G[XH](\d{2})(\d{4})\.MP4$", re.IGNORECASE)

# profils d'encodage. `preview` sert au POC : basse resolution, rapide.
PROFILES = {
    "preview": ["-c:v", "h264_nvenc", "-preset", "p1", "-cq", "30", "-b:v", "0",
                "-c:a", "aac", "-b:a", "128k"],
    "archive": ["-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "24",
                "-b:v", "0", "-c:a", "copy", "-tag:v", "hvc1"],
    "x264":    ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac", "-b:a", "128k"],
}
PROFILE_SCALE = {"preview": 960}


def check_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        log.error("outils introuvables dans PATH : %s", ", ".join(missing))
        sys.exit(2)


def probe(f: Path):
    """(duree, largeur, hauteur) du fichier. Zeros si illisible."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(f)],
            capture_output=True, text=True, check=True,
        ).stdout
        data = json.loads(out)
        dur = float(data.get("format", {}).get("duration", 0.0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return dur, int(s["width"]), int(s["height"])
        return dur, 0, 0
    except (subprocess.CalledProcessError, ValueError, KeyError,
            json.JSONDecodeError):
        return 0.0, 0, 0


def probe_duration(f: Path) -> float:
    return probe(f)[0]


@dataclass
class Recording:
    """Un enregistrement = les chapitres GoPro d'un meme index, dans l'ordre."""
    index: str
    chapters: list = field(default_factory=list)   # [Path], ordonnes
    durations: list = field(default_factory=list)  # [float], meme ordre
    width: int = 0
    height: int = 0

    @property
    def duration(self) -> float:
        return sum(self.durations)

    @property
    def name(self) -> str:
        return f"recording_{self.index}"

    def offsets(self):
        """Debut de chaque chapitre sur la timeline concatenee."""
        out, cum = [], 0.0
        for d in self.durations:
            out.append(cum)
            cum += d
        return out


def discover(dump: Path):
    """Regroupe les .MP4 GoPro par index d'enregistrement."""
    groups = {}
    for clip in sorted(dump.glob("*.MP4")):
        m = CLIP_RE.match(clip.name)
        if not m:
            log.warning("nom non-GoPro ignore : %s", clip.name)
            continue
        chapter, index = m.group(1), m.group(2)
        groups.setdefault(index, {})[chapter] = clip

    out = []
    for index in sorted(groups):
        chapters = [groups[index][c] for c in sorted(groups[index])]
        probes = [probe(c) for c in chapters]
        w, h = next(((w, h) for _, w, h in probes if w), (0, 0))
        out.append(Recording(index, chapters, [d for d, _, _ in probes], w, h))
    return out


def extract_gpmd(mp4: Path, dest: Path) -> Path:
    """Extrait le flux de donnees `gpmd`. Rapide : ffmpeg ne lit que ces samples."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    idx = _gpmd_index(mp4)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(mp4),
                    "-map", f"0:{idx}", "-c", "copy", "-f", "data", str(dest)],
                   check=True)
    return dest


def _gpmd_index(mp4: Path) -> int:
    """Index du flux gpmd (repere par son codec_tag, pas par sa position)."""
    out = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", str(mp4)],
        capture_output=True, text=True, check=True).stdout
    for s in json.loads(out).get("streams", []):
        if s.get("codec_tag_string") == "gpmd":
            return s["index"]
    raise ValueError(f"{mp4.name} : pas de flux gpmd")


def write_concat_list(chapters, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("".join(f"file '{c.as_posix()}'\n" for c in chapters), encoding="utf-8")
    return dest


def build_command(concat_list: Path, dest: Path, ass: Path = None,
                  profile="preview", start=None, duration=None, scale=None,
                  blur=None, blur_sigma=0.0, fonts: Path = None):
    """Commande ffmpeg de rendu. `ass` et `concat_list` sont references par leur
    nom seul : ffmpeg est lance depuis leur dossier, ce qui evite d'avoir a
    echapper les `:` et `\\` des chemins Windows dans la chaine de filtres.

    `blur` : rectangles (x, y, w, h) a flouter avant l'incrustation. C'est
    l'equivalent du `backdrop-filter: blur()` du design, que le format ASS ne
    sait pas rendre : on floute la zone sous chaque panneau, le panneau
    translucide etant ensuite dessine par-dessus par libass.
    """
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostats",
           "-progress", "pipe:1", "-y"]
    if start:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-f", "concat", "-safe", "0", "-i", concat_list.name]
    if duration:
        cmd += ["-t", f"{duration:.3f}"]

    width = scale if scale is not None else PROFILE_SCALE.get(profile)
    steps = []
    src = "0:v:0"
    if width:
        steps.append(f"[{src}]scale={width}:-2[sc]")
        src = "sc"

    if blur and blur_sigma > 0:
        outs = [f"bg{i}" for i in range(len(blur))]
        steps.append(f"[{src}]split={len(blur) + 1}[base]"
                     + "".join(f"[{o}]" for o in outs))
        cur = "base"
        for i, ((x, y, w, h), tap) in enumerate(zip(blur, outs)):
            steps.append(f"[{tap}]crop={w}:{h}:{x}:{y},"
                         f"gblur=sigma={blur_sigma:.2f}[bl{i}]")
            nxt = f"ov{i}"
            steps.append(f"[{cur}][bl{i}]overlay={x}:{y}[{nxt}]")
            cur = nxt
        src = cur

    if ass:
        opt = f"ass={ass.name}"
        if fonts and fonts.is_dir():
            opt += f":fontsdir={fonts.name}"
        steps.append(f"[{src}]{opt}[v]")
        src = "v"

    if steps:
        cmd += ["-filter_complex", ";".join(steps), "-map", f"[{src}]"]
    else:
        cmd += ["-map", "0:v:0"]
    cmd += ["-map", "0:a:0?"]
    cmd += PROFILES[profile]
    cmd += [str(dest)]
    return cmd


def run(cmd, cwd: Path, total: float = 0.0, label: str = "") -> bool:
    """Lance ffmpeg en affichant l'avancement. `cwd` doit contenir la liste de
    concatenation et le .ass, references par leur nom seul dans `cmd`."""
    proc = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    t0 = time.monotonic()
    speed = 0.0
    tty = sys.stderr.isatty()
    drawn = False
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("speed="):
            s = line.split("=", 1)[1].rstrip("x")
            try:
                speed = float(s)
            except ValueError:
                pass
            continue
        if not line.startswith("out_time_ms=") or not tty:
            continue
        try:
            done = int(line.split("=", 1)[1]) / 1_000_000
        except ValueError:
            continue
        if total > 0:
            pct = max(0, min(100, int(done * 100 / total)))
            bar = "#" * (pct * 26 // 100) + "-" * (26 - pct * 26 // 100)
            sys.stderr.write(f"\r    {label} [{bar}] {pct:3d}%  "
                             f"{done:6.1f}/{total:.0f}s  {speed:4.1f}x")
        else:
            sys.stderr.write(f"\r    {label} {done:6.1f}s  {speed:4.1f}x")
        sys.stderr.flush()
        drawn = True
    proc.wait()
    if drawn:
        sys.stderr.write("\n")
    if proc.returncode != 0:
        for ln in (proc.stderr.read() or "").splitlines()[-10:]:
            log.error("ffmpeg: %s", ln)
        return False
    log.info("%s : rendu en %.0fs", label, time.monotonic() - t0)
    return True
