import json
import logging
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

BASE    = Path(__file__).resolve().parent
INPUT   = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else BASE / "dump"
OUTPUT  = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else BASE / "output"
CQ      = 24
BAR_LEN = 30

CLIP_RE = re.compile(r"^G[XH](\d{2})(\d{4})\.MP4$", re.IGNORECASE)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("archive")


def check_tools():
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        log.error("outils introuvables dans PATH : %s", ", ".join(missing))
        sys.exit(2)


def probe_duration(f: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", str(f)],
            capture_output=True, text=True, check=True,
        ).stdout
        return float(json.loads(out).get("format", {}).get("duration", 0.0))
    except (subprocess.CalledProcessError, ValueError, json.JSONDecodeError):
        return 0.0


def human_time(sec: float) -> str:
    if sec < 1:
        return "0s"
    if sec < 60:
        return f"{sec:.0f}s"
    m, s = divmod(int(sec), 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _drain(pipe, buf):
    for line in pipe:
        buf.append(line)


def run_ffmpeg_with_progress(cmd, total_us: int, prefix: str):
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    stderr_buf = []
    stderr_th = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf), daemon=True)
    stderr_th.start()

    tty = sys.stderr.isatty()
    speed = 0.0
    last_pct_logged = -1
    bar_drawn = False

    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("speed="):
                s = line.split("=", 1)[1]
                if s.endswith("x"):
                    try:
                        speed = float(s[:-1])
                    except ValueError:
                        speed = 0.0
                continue
            if not line.startswith("out_time_ms="):
                continue
            try:
                us = int(line.split("=", 1)[1])
            except ValueError:
                continue

            elapsed_s = us / 1_000_000
            if total_us > 0:
                pct = max(0, min(100, us * 100 // total_us))
                total_s = total_us / 1_000_000
                eta = (total_s - elapsed_s) / speed if speed > 0.01 else 0
                if tty:
                    filled = pct * BAR_LEN // 100
                    bar = "#" * filled + "-" * (BAR_LEN - filled)
                    text = (f"\r    {prefix} [{bar}] {pct:3d}%  "
                            f"{human_time(elapsed_s)}/{human_time(total_s)}  "
                            f"{speed:5.1f}x  ETA {human_time(eta):>8}")
                    sys.stderr.write(text)
                    sys.stderr.flush()
                    bar_drawn = True
                elif pct >= last_pct_logged + 10:
                    log.info("    %s : %d%% (%s/%s, %.1fx)",
                             prefix, pct, human_time(elapsed_s),
                             human_time(total_s), speed)
                    last_pct_logged = pct - (pct % 10)
            else:
                if tty:
                    text = f"\r    {prefix} encode... {human_time(elapsed_s)}  {speed:5.1f}x"
                    sys.stderr.write(text)
                    sys.stderr.flush()
                    bar_drawn = True

        rc = proc.wait()
        stderr_th.join(timeout=1)
        if bar_drawn:
            sys.stderr.write("\n")
            sys.stderr.flush()
        if rc != 0:
            err = "".join(stderr_buf).strip()
            if err:
                for ln in err.splitlines()[-10:]:
                    log.error("    ffmpeg: %s", ln)
            raise subprocess.CalledProcessError(rc, cmd)
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        if bar_drawn:
            sys.stderr.write("\n")
        raise
    finally:
        if proc.poll() is None:
            proc.terminate()


def encode_recording(index: str, clips, outdir: Path, n: int, total: int):
    archive = outdir / f"recording_{index}.mp4"
    tag = f"[{n}/{total}] {archive.name}"

    duration = sum(probe_duration(c) for c in clips)

    if archive.exists():
        log.info("%s : skip (existe deja, %d chapitre(s), ~%s)",
                 tag, len(clips), human_time(duration))
        return True, duration

    log.info("%s : %d chapitre(s), ~%s -> encode",
             tag, len(clips), human_time(duration))
    for i, c in enumerate(clips, 1):
        log.info("    chapitre %d/%d : %s", i, len(clips), c.name)

    lst = outdir / f".concat_{index}.txt"
    t0 = time.monotonic()
    try:
        lst.write_text("".join(f"file '{c.as_posix()}'\n" for c in clips))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "fatal", "-nostats",
               "-progress", "pipe:1",
               "-f", "concat", "-safe", "0", "-i", str(lst),
               "-map", "0:v:0", "-map", "0:a:0?",
               "-c:v", "hevc_nvenc", "-preset", "p6", "-rc", "vbr",
               "-cq", str(CQ), "-b:v", "0",
               "-c:a", "copy", "-tag:v", "hvc1", str(archive)]
        run_ffmpeg_with_progress(cmd, int(duration * 1_000_000), f"[{n}/{total}]")

        elapsed = time.monotonic() - t0
        speed = duration / elapsed if elapsed > 0 and duration > 0 else 0.0
        speed_txt = f", {speed:.1f}x realtime" if speed else ""
        size_mb = archive.stat().st_size / (1024 * 1024)
        log.info("%s : OK en %s%s (%.0f MB)", tag, human_time(elapsed), speed_txt, size_mb)
        return True, duration
    except subprocess.CalledProcessError as e:
        elapsed = time.monotonic() - t0
        log.error("%s : FFmpeg a echoue apres %s (code %s)",
                  tag, human_time(elapsed), e.returncode)
        if archive.exists():
            try:
                archive.unlink()
                log.info("%s : archive partielle supprimee", tag)
            except OSError as ue:
                log.warning("%s : nettoyage archive partielle : %s", tag, ue)
        return False, duration
    finally:
        if lst.exists():
            try:
                lst.unlink()
            except OSError as ue:
                log.warning("%s : nettoyage %s : %s", tag, lst.name, ue)


def main():
    log.info("demarrage : INPUT=%s OUTPUT=%s CQ=%d", INPUT, OUTPUT, CQ)
    check_tools()

    if not INPUT.is_dir():
        log.error("dossier d'entree introuvable : %s", INPUT)
        sys.exit(2)

    log.info("scan de %s ...", INPUT)
    groups = defaultdict(dict)
    unmatched = 0
    for clip in INPUT.rglob("*.MP4"):
        m = CLIP_RE.match(clip.name)
        if not m:
            log.warning("nom non-GoPro ignore : %s", clip.relative_to(INPUT))
            unmatched += 1
            continue
        chapter, index = m.group(1), m.group(2)
        existing = groups[index].get(chapter)
        if existing is not None:
            log.warning("doublon index=%s chapitre=%s : %s ignore (garde %s)",
                        index, chapter, clip, existing)
            continue
        groups[index][chapter] = clip

    if not groups:
        log.warning("aucun .MP4 GoPro trouve dans %s", INPUT)
        return

    total_chapters = sum(len(v) for v in groups.values())
    log.info("%d enregistrement(s) detecte(s) sur %d chapitre(s)%s",
             len(groups), total_chapters,
             f" ({unmatched} fichier(s) hors format ignore(s))" if unmatched else "")
    for idx in sorted(groups):
        log.info("  - recording_%s : %d chapitre(s)", idx, len(groups[idx]))

    OUTPUT.mkdir(parents=True, exist_ok=True)
    total = len(groups)
    ok = fail = 0
    total_source_duration = 0.0
    t_start = time.monotonic()

    for n, index in enumerate(sorted(groups), 1):
        clips = [groups[index][ch] for ch in sorted(groups[index])]
        success, duration = encode_recording(index, clips, OUTPUT, n, total)
        total_source_duration += duration
        if success:
            ok += 1
        else:
            fail += 1

    t_total = time.monotonic() - t_start
    avg = total_source_duration / t_total if t_total > 0 and total_source_duration > 0 else 0.0
    avg_txt = f", moyenne {avg:.1f}x realtime" if avg else ""
    log.info("termine : %d ok, %d echec(s) - duree totale %s pour %s de rush%s",
             ok, fail, human_time(t_total), human_time(total_source_duration), avg_txt)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
