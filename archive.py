import json
import logging
import msvcrt
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

BASE         = Path(__file__).resolve().parent
INPUT        = Path(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1] else BASE / "dump"
OUTPUT       = Path(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2] else BASE / "output"
CQ           = 24
BAR_LEN      = 26
TARGET_SPEED = 1.4  # ratio realtime attendu, sert a l'estimation ETA globale

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


def _keyboard_watcher(proc, skip_event, stop_event, out_lock, bar_cleaned):
    try:
        while msvcrt.kbhit():
            msvcrt.getwch()
        while not stop_event.is_set():
            if msvcrt.kbhit():
                key = msvcrt.getwch()
                if key in ("\x00", "\xe0"):
                    msvcrt.getwch()
                    continue
                if not key or key.lower() != "s":
                    continue
                with out_lock:
                    sys.stderr.write("\n    Skip l'enregistrement en cours ? (o/N) ")
                    sys.stderr.flush()
                    bar_cleaned.set()
                    while True:
                        ans = msvcrt.getwch()
                        if ans in ("\x00", "\xe0"):
                            msvcrt.getwch()
                            continue
                        if not ans:
                            continue
                        if ans.lower() == "o":
                            sys.stderr.write("oui - arret de FFmpeg\n")
                            sys.stderr.flush()
                            skip_event.set()
                            try:
                                proc.terminate()
                            except OSError:
                                pass
                            return
                        if ans.lower() == "n" or ans in ("\r", "\x1b"):
                            sys.stderr.write("annule, poursuite\n")
                            sys.stderr.flush()
                            while msvcrt.kbhit():
                                msvcrt.getwch()
                            break
            time.sleep(0.05)
    except Exception:
        pass


def run_ffmpeg_with_progress(cmd, total_us: int, prefix: str, status_prefix_fn=None) -> str:
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, bufsize=1,
    )
    stderr_buf = []
    stderr_th = threading.Thread(target=_drain, args=(proc.stderr, stderr_buf), daemon=True)
    stderr_th.start()

    out_lock = threading.Lock()
    skip_event = threading.Event()
    stop_kb = threading.Event()
    bar_cleaned = threading.Event()
    kb_th = None
    if sys.stdin.isatty():
        kb_th = threading.Thread(
            target=_keyboard_watcher,
            args=(proc, skip_event, stop_kb, out_lock, bar_cleaned),
            daemon=True,
        )
        kb_th.start()

    tty = sys.stderr.isatty()
    speed = 0.0
    last_pct_logged = -1
    bar_drawn = False

    try:
        for line in proc.stdout:
            if skip_event.is_set():
                break
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
            gprefix = status_prefix_fn(us) if status_prefix_fn else ""
            if total_us > 0:
                pct = max(0, min(100, us * 100 // total_us))
                total_s = total_us / 1_000_000
                if tty:
                    filled = pct * BAR_LEN // 100
                    bar = "#" * filled + "-" * (BAR_LEN - filled)
                    text = (f"\r    {gprefix}  {prefix} [{bar}] {pct:3d}%  "
                            f"{human_time(elapsed_s)}/{human_time(total_s)}  "
                            f"{speed:4.1f}x   s=skip")
                    with out_lock:
                        sys.stderr.write(text)
                        sys.stderr.flush()
                    bar_drawn = True
                elif pct >= last_pct_logged + 10:
                    log.info("    %s %s : %d%% (%s/%s, %.1fx)",
                             gprefix, prefix, pct, human_time(elapsed_s),
                             human_time(total_s), speed)
                    last_pct_logged = pct - (pct % 10)
            else:
                if tty:
                    text = (f"\r    {gprefix}  {prefix} encode... "
                            f"{human_time(elapsed_s)}  {speed:4.1f}x   s=skip")
                    with out_lock:
                        sys.stderr.write(text)
                        sys.stderr.flush()
                    bar_drawn = True

        if skip_event.is_set():
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        else:
            proc.wait()
        rc = proc.returncode
        stderr_th.join(timeout=1)

        if bar_drawn and not bar_cleaned.is_set():
            with out_lock:
                sys.stderr.write("\n")
                sys.stderr.flush()

        if skip_event.is_set():
            return "skipped"
        if rc != 0:
            err = "".join(stderr_buf).strip()
            if err:
                for ln in err.splitlines()[-10:]:
                    log.error("    ffmpeg: %s", ln)
            return "failed"
        return "ok"
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()
        if bar_drawn and not bar_cleaned.is_set():
            sys.stderr.write("\n")
        raise
    finally:
        stop_kb.set()
        if proc.poll() is None:
            proc.terminate()


def encode_recording(index, clips, duration, outdir, n, total, source_done, source_total):
    archive = outdir / f"recording_{index}.mp4"
    tag = f"[{n}/{total}] {archive.name}"
    tolerance = 2.0  # secondes

    if archive.exists():
        actual = probe_duration(archive)
        if duration > 0:
            if abs(actual - duration) <= tolerance:
                log.info("%s : deja present et complet (%s ~ source %s), skip",
                         tag, human_time(actual), human_time(duration))
                return "already_present", duration
            log.warning("%s : archive existante incomplete "
                        "(duree %s vs source %s attendu, ecart %s) - re-encode",
                        tag, human_time(actual), human_time(duration),
                        human_time(abs(actual - duration)))
            try:
                archive.unlink()
            except OSError as e:
                log.error("%s : impossible de supprimer l'archive incomplete : %s", tag, e)
                return "failed", 0.0
        else:
            log.warning("%s : deja present mais duree source inconnue, "
                        "impossible de verifier - skip (a controler manuellement)", tag)
            return "already_present", 0.0

    est_encode = duration / TARGET_SPEED if duration > 0 else 0
    remaining_before = source_total - source_done
    eta_before = remaining_before / TARGET_SPEED if remaining_before > 0 else 0
    pct_before = (source_done * 100 / source_total) if source_total > 0 else 0

    log.info("%s : %d chapitre(s), source %s -> encode estime ~%s (ratio %.1fx)",
             tag, len(clips), human_time(duration),
             human_time(est_encode), TARGET_SPEED)
    log.info("    global avant encode : %s / %s (%.1f%%), ETA restant ~%s",
             human_time(source_done), human_time(source_total),
             pct_before, human_time(eta_before))
    for i, c in enumerate(clips, 1):
        log.info("    chapitre %d/%d : %s", i, len(clips), c.name)

    def status_prefix(cur_us: int) -> str:
        if source_total <= 0:
            return ""
        cur = source_done + cur_us / 1_000_000
        pct = min(100.0, cur * 100 / source_total)
        rem = source_total - cur
        eta = rem / TARGET_SPEED if rem > 0 else 0
        return f"G {pct:4.1f}% ETA {human_time(eta):>8}"

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
        status = run_ffmpeg_with_progress(
            cmd, int(duration * 1_000_000), f"[{n}/{total}]",
            status_prefix_fn=status_prefix,
        )

        elapsed = time.monotonic() - t0
        if status == "ok":
            speed = duration / elapsed if elapsed > 0 and duration > 0 else 0.0
            speed_txt = f", {speed:.1f}x realtime" if speed else ""
            size_mb = archive.stat().st_size / (1024 * 1024)
            log.info("%s : OK en %s%s (%.0f MB)", tag, human_time(elapsed), speed_txt, size_mb)
            return "ok", duration
        if status == "skipped":
            log.info("%s : SKIPPED apres %s", tag, human_time(elapsed))
            if archive.exists():
                try:
                    archive.unlink()
                    log.info("%s : archive partielle supprimee", tag)
                except OSError as ue:
                    log.warning("%s : nettoyage archive partielle : %s", tag, ue)
            return "skipped", 0.0
        log.error("%s : FFmpeg a echoue apres %s", tag, human_time(elapsed))
        if archive.exists():
            try:
                archive.unlink()
                log.info("%s : archive partielle supprimee", tag)
            except OSError as ue:
                log.warning("%s : nettoyage archive partielle : %s", tag, ue)
        return "failed", 0.0
    finally:
        if lst.exists():
            try:
                lst.unlink()
            except OSError as ue:
                log.warning("%s : nettoyage %s : %s", tag, lst.name, ue)


def main():
    log.info("demarrage : INPUT=%s OUTPUT=%s CQ=%d ratio_estime=%.1fx",
             INPUT, OUTPUT, CQ, TARGET_SPEED)
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
    log.info("probe des durees (%d fichier(s)) ...", total_chapters)
    durations = {}
    for idx in sorted(groups):
        durations[idx] = sum(probe_duration(p) for p in groups[idx].values())

    total_source = sum(durations.values())
    est_total_encode = total_source / TARGET_SPEED if total_source > 0 else 0

    log.info("%d enregistrement(s) sur %d chapitre(s)%s",
             len(groups), total_chapters,
             f" ({unmatched} fichier(s) hors format ignore(s))" if unmatched else "")
    log.info("duree source totale : %s  ->  encodage total estime : %s (ratio %.1fx)",
             human_time(total_source), human_time(est_total_encode), TARGET_SPEED)
    for idx in sorted(groups):
        est = durations[idx] / TARGET_SPEED if durations[idx] > 0 else 0
        log.info("  - recording_%s : %d chapitre(s), source %s -> encode ~%s",
                 idx, len(groups[idx]), human_time(durations[idx]), human_time(est))

    log.info("astuce : appuyer sur 's' pendant l'encodage pour skipper l'enregistrement en cours (avec confirmation)")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    total = len(groups)
    ok = fail = skipped = already = 0
    source_done = 0.0
    t_start = time.monotonic()

    for n, index in enumerate(sorted(groups), 1):
        clips = [groups[index][ch] for ch in sorted(groups[index])]
        status, done_dur = encode_recording(
            index, clips, durations[index], OUTPUT,
            n, total, source_done, total_source,
        )
        source_done += done_dur
        if status == "ok":
            ok += 1
        elif status == "already_present":
            already += 1
        elif status == "skipped":
            skipped += 1
        else:
            fail += 1

    t_total = time.monotonic() - t_start
    avg = source_done / t_total if t_total > 0 and source_done > 0 else 0.0
    avg_txt = f", moyenne {avg:.1f}x realtime" if avg else ""
    log.info("termine : %d ok, %d deja present(s), %d skipped, %d echec(s) - "
             "duree totale %s pour %s de rush encode%s",
             ok, already, skipped, fail,
             human_time(t_total), human_time(source_done), avg_txt)
    if fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
