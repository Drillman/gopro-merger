"""Orchestration : chrono -> appariement video -> concatenation -> incrustation -> rendu.

Les videos finales sont deposees a la racine du dossier de sortie, nommees
« AAAA-MM-JJ - session N.mp4 ». Les fichiers intermediaires (liste de concat,
sous-titres ASS, cache de telemetrie) vivent dans `work/`, hors de output/.
"""

import argparse
import logging
import re
import sys
from collections import defaultdict
from pathlib import Path

from . import DUMP, OUTPUT, WORK, ra1, overlay, sync, video

log = logging.getLogger("roulage")

PAD = 3.0          # secondes conservees de part et d'autre d'un extrait --tour


def session_numbers(sessions):
    """Numero de session dans sa journee, par ordre chronologique.

    C'est ce numero qui nomme les videos : il parle plus que l'heure de fin
    portee par le nom du .ra1.
    """
    out = {}
    by_day = defaultdict(list)
    for s in sessions:
        if s.start_time is not None:
            by_day[s.start_time.date()].append(s)
    for day, group in by_day.items():
        for i, s in enumerate(sorted(group, key=lambda x: x.start_time), 1):
            out[s.path] = (day, i)
    return out


def output_name(session, numbers, lap=None):
    """Nom du fichier final : « 2026-07-26 - session 2.mp4 »."""
    entry = numbers.get(session.path)
    stem = (f"{entry[0]:%Y-%m-%d} - session {entry[1]}" if entry
            else session.path.stem)
    return stem + (f" - tour {lap}" if lap else "")


def work_name(session, numbers, lap=None):
    """Nom des fichiers intermediaires : sans espace.

    Ils sont cites tels quels dans le graphe de filtres ffmpeg, ou les espaces
    et la ponctuation sont autant d'occasions de casser l'analyse syntaxique.
    Seul le .mp4 final porte le nom lisible.
    """
    entry = numbers.get(session.path)
    stem = (f"{entry[0]:%Y%m%d}_session{entry[1]}" if entry
            else re.sub(r"\W+", "_", session.path.stem))
    return stem + (f"_tour{lap}" if lap else "")


def check_single_circuit(sessions):
    """Le dump ne doit couvrir qu'un circuit a la fois.

    Le trace de la carte, les secteurs et la ligne d'arrivee sont deduits d'une
    session de reference : melanger deux circuits produirait une carte fausse
    sans aucun message d'erreur.
    """
    named = {(s.track_id, s.circuit) for s in sessions if s.finalized}
    if len(named) > 1:
        raise sync.SyncError(
            "le dump melange plusieurs circuits : "
            + ", ".join(f"{c} (id {i})" for i, c in sorted(named))
            + ". Traiter une journee et un circuit a la fois.")
    return next(iter(named), None)


def setup_logging():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")


def report(result: sync.SyncResult):
    r = result
    log.info("%s <- %s : t_video = t_chrono %+.2fs (r=%.4f, ecart %.1f m)",
             r.session.path.stem, r.recording.name, r.offset, r.r_speed, r.error)
    if r.per_lap:
        log.info("    decalage par tour : %s  (dispersion %.2fs)",
                 "  ".join(f"T{n}{o:+.2f}s/{e:.1f}m" for n, o, e in r.per_lap),
                 r.spread)
    for line in r.diagnosis():
        log.error("    %s", line)
    if r.usable and not r.healthy:
        log.warning("    calage exploitable mais imparfait : timeline video "
                    "possiblement irreguliere ou GPS GoPro degrade")
    for num, lap, t0, t1, state in sync.coverage(r):
        if state != "complet":
            log.warning("    tour %d (%s) : %s dans %s",
                        num, ra1.fmt_lap(lap), state, r.recording.name)


def build(result: sync.SyncResult, outdir: Path, workdir: Path, numbers,
          profile="preview", lap=None, width=None, dry_run=False,
          keep_pits=False, blur=True):
    """Concatene, incruste et encode la fenetre demandee."""
    r = result
    total = r.recording.duration
    cross, laps = ra1.laps(r.session)

    keep = list(range(len(laps))) if keep_pits else ra1.flying_laps(r.session)
    if not keep:
        log.error("%s : aucun tour exploitable", r.session.path.stem)
        return None
    dropped = [k + 1 for k in range(len(laps)) if k not in keep]
    if dropped:
        log.info("tours ecartes (sortie/rentree stands) : %s",
                 ", ".join(f"n°{k} ({ra1.fmt_lap(laps[k - 1])})" for k in dropped))

    if lap is not None:
        # --tour reste un extrait : c'est le seul cas ou l'on rogne
        if not 1 <= lap <= len(keep):
            log.error("tour %d inexistant (1..%d)", lap, len(keep))
            return None
        k = keep[lap - 1]
        start = r.video_time(cross[k]) - PAD
        end = r.video_time(cross[k + 1]) + PAD
    else:
        # l'enregistrement est conserve entier ; hors des tours retenus le HUD
        # bascule en veille plutot que de couper l'image
        start, end = 0.0, total
    start = max(0.0, start)
    end = min(total, end)
    if end - start < 1.0:
        log.error("%s : la video ne couvre pas la fenetre demandee", r.recording.name)
        return None
    wanted = r.video_time(cross[keep[-1] + 1])
    if total < wanted:
        log.warning("video plus courte que la session : %.0fs de roulage sans image",
                    wanted - total)

    # les videos finales vont a la racine de `outdir` ; tout l'intermediaire
    # (liste de concat, sous-titres ASS) part dans work/, hors du dossier suivi
    work = workdir
    work.mkdir(parents=True, exist_ok=True)
    dest = outdir / f"{output_name(r.session, numbers, lap)}.mp4"
    stem = work_name(r.session, numbers, lap)

    # `width` a None = resolution native du rush : c'est ce que veut le profil
    # archive. La hauteur suit le format reel du rush, pas un 16/9 presume.
    src_w, src_h = r.recording.width, r.recording.height
    if not src_w or not src_h:
        log.error("%s : resolution video illisible", r.recording.name)
        return None
    if width is None:
        width, height = src_w, src_h
        scale = None
    else:
        height = round(width * src_h / src_w / 2) * 2
        scale = width
    ass = overlay.write(r.session, r.offset, work / f"{stem}.ass",
                        width, height, t0=start, t1=end, keep=keep)
    lst = video.write_concat_list(r.recording.chapters, work / f"{stem}.concat")
    regions = overlay.panel_regions(width, height) if blur else None
    fonts = Path(overlay.__file__).parent / "fonts"
    cmd = video.build_command(lst, dest, ass=ass, profile=profile,
                              start=start, duration=end - start, scale=scale,
                              blur=regions, blur_sigma=8.0 * height / overlay.REF_H,
                              fonts=fonts if fonts.is_dir() else None)

    log.info("fenetre video %.1fs -> %.1fs (%.0fs) en %dx%d, profil %s ; %s -> %s",
             start, end, end - start, width, height, profile,
             " + ".join(c.name for c in r.recording.chapters), dest.name)
    if dry_run:
        log.info("commande : %s", " ".join(cmd))
        return dest
    ok = video.run(cmd, cwd=work, total=end - start, label=dest.name)
    return dest if ok else None


def main(argv=None):
    setup_logging()
    ap = argparse.ArgumentParser(
        description="Concatene une session et y incruste les donnees du chrono 3DMS.")
    ap.add_argument("--gps", type=Path, default=DUMP,
                    help="dossier des .ra1 (defaut : dump/, a cote des rushs)")
    ap.add_argument("--rushs", type=Path, default=DUMP, help="dossier des .MP4 GoPro")
    ap.add_argument("--work", type=Path, default=WORK,
                    help="dossier des fichiers temporaires (defaut : work/)")
    ap.add_argument("--out", type=Path, default=OUTPUT,
                    help="dossier des videos finales (defaut : output/)")
    ap.add_argument("--session", help="nom (ou fragment) de la session ; "
                                      "par defaut la plus courte disposant d'une video")
    ap.add_argument("--tour", type=int,
                    help="ne rendre qu'un tour, numerote parmi les tours retenus")
    ap.add_argument("--tours-stands", action="store_true",
                    help="conserver les tours de sortie et de rentree des stands")
    ap.add_argument("--profil", choices=sorted(video.PROFILES), default="preview",
                    help="preview = apercu rapide 960px ; archive = HEVC NVENC "
                         "en resolution native, qualite de conservation")
    ap.add_argument("--largeur", type=int, default=None,
                    help="largeur de sortie ; par defaut celle du profil "
                         "(960 en preview, native en archive)")
    ap.add_argument("--utc", type=int, default=None, metavar="H",
                    help="decalage horaire des noms de .ra1 ; deduit des donnees "
                         "par defaut")
    ap.add_argument("--force", action="store_true",
                    help="produire la video meme si le calage est juge peu fiable")
    ap.add_argument("--toutes", action="store_true", help="traiter toutes les sessions")
    ap.add_argument("--sans-flou", action="store_true",
                    help="ne pas flouter le fond sous les panneaux du HUD")
    ap.add_argument("--dry-run", action="store_true", help="afficher la commande sans encoder")
    a = ap.parse_args(argv)

    video.check_tools()
    sessions = [ra1.load(p) for p in sorted(a.gps.glob("*.ra1"))]
    if not sessions:
        log.error("aucun .ra1 dans %s", a.gps)
        return 2
    recordings = video.discover(a.rushs)
    if not recordings:
        log.error("aucun rush GoPro dans %s", a.rushs)
        return 2

    try:
        circuit = check_single_circuit(sessions)
        workdir = a.work / "gpmd"
        pairs, _ = sync.match(sessions, recordings, workdir, a.utc)
    except sync.SyncError as e:
        log.error("%s", e)
        return 1

    log.info("%d session(s) chrono, %d enregistrement(s) video%s",
             len(sessions), len(recordings),
             f", circuit {circuit[1]}" if circuit else "")

    results = []
    for session, rec, track in pairs:
        try:
            results.append(sync.align(session, rec, track))
        except ValueError as e:
            log.warning("%s", e)
    for r in results:
        report(r)

    if not a.force:
        rejected = [r for r in results if not r.usable]
        for r in rejected:
            log.error("%s : calage refuse, session ecartee (--force pour passer outre)",
                      r.session.path.stem)
        results = [r for r in results if r.usable]
    if not results:
        log.error("aucune session ne presente un calage exploitable")
        return 1

    if a.session:
        chosen = [r for r in results if a.session.lower() in r.session.path.stem.lower()]
        if not chosen:
            log.error("aucune session appariee ne correspond a %r", a.session)
            return 2
    elif a.toutes:
        chosen = results
    else:
        chosen = [min(results, key=lambda r: r.session.duration)]
        log.info("session la plus courte avec video : %s (%.0fs)",
                 chosen[0].session.path.stem, chosen[0].session.duration)

    numbers = session_numbers(sessions)
    rc = 0
    for r in chosen:
        width = a.largeur if a.largeur is not None else video.PROFILE_SCALE.get(a.profil)
        if build(r, a.out, a.work, numbers, a.profil, a.tour, width,
                 a.dry_run, a.tours_stands, not a.sans_flou) is None:
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
