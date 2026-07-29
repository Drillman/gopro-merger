"""Appariement session chrono <-> enregistrement GoPro, et recalage temporel.

Le chrono 3DMS est la source de verite : c'est lui qui fournit les donnees
affichees. Le GPS de la GoPro ne sert qu'a determiner ou tombe chaque instant
chrono sur la timeline video.

Le recalage se fait en trois etapes, dans cet ordre :

  1. GPSU -> quelle session correspond a quel enregistrement (a la seconde pres) ;
  2. correlation du PROFIL DE VITESSE -> leve l'ambiguite de tour entier. Sur un
     circuit ferme la trajectoire est quasi identique d'un tour a l'autre, et un
     recalage sur la seule position se verrouille volontiers un tour a cote avec
     un ecart parfaitement credible ;
  3. minimisation de l'ecart de POSITION sur +/-2 s -> precision finale (~0,02 s).

`SyncResult.per_lap` donne le decalage reoptimise tour par tour : sa dispersion
est l'indicateur de sante du calage (voir `SyncResult.healthy`).
"""

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import ra1, gpmf, video

log = logging.getLogger("trackday.sync")

HZ = 10.0
MIN_SPEED = 40.0     # km/h : on ne recale que sur les portions roulees

# Seuils de recevabilite du calage. En deca, mieux vaut refuser que livrer une
# video ou l'incrustation est decalee sans que ca se voie au premier coup d'oeil.
MIN_R = 0.95         # correlation des profils de vitesse
MAX_ERROR = 15.0     # ecart de position median, en metres
MAX_SPREAD = 1.5     # dispersion du decalage entre tours, en secondes


class SyncError(Exception):
    """Incoherence entre les donnees chrono et les rushs GoPro."""


def naive_epoch(dt):
    """Datetime sans fuseau (nom de fichier .ra1) -> epoch, lu comme de l'UTC."""
    return dt.replace(tzinfo=timezone.utc).timestamp()


def detect_utc_offset(sessions, anchors):
    """Decalage horaire, en heures, entre les noms de .ra1 et l'UTC des GoPro.

    Les .ra1 sont nommes en heure locale et ne contiennent aucune date ; les
    GoPro fournissent de l'UTC satellite. Plutot que de figer un fuseau (ce qui
    casserait a l'heure d'hiver ou sur un circuit a l'etranger), on retient le
    decalage entier qui fait le mieux recouvrir sessions et enregistrements.
    """
    best = (0, -1.0)
    for off in range(-12, 15):
        total = 0.0
        for utc0, duration in anchors:
            covered = 0.0
            for s in sessions:
                if s.start_time is None:
                    continue
                st = naive_epoch(s.start_time) - off * 3600
                covered = max(covered,
                              min(utc0 + duration, st + s.duration) - max(utc0, st))
            total += max(0.0, covered)
        if total > best[1]:
            best = (off, total)
    return best[0]


def median(v):
    v = sorted(v)
    return v[len(v) // 2] if v else float("nan")


def recording_track(rec: video.Recording, workdir):
    """Echantillons GPS d'un enregistrement, sur la timeline concatenee."""
    track = []
    for chapter, dur, off in zip(rec.chapters, rec.durations, rec.offsets()):
        binpath = video.extract_gpmd(chapter, workdir / f"{chapter.stem}.gpmd")
        for s in gpmf.gps_track(binpath.read_bytes(), dur):
            s.t += off
            track.append(s)
    return gpmf.clean(track)


class Trajectory:
    """Position interpolee a un instant, en metres dans un repere local."""

    def __init__(self, times, lat, lon, lat0, lon0):
        self.t = times
        kx = 111320.0 * math.cos(math.radians(lat0))
        self.x = [(v - lon0) * kx for v in lon]
        self.y = [(v - lat0) * 110540.0 for v in lat]
        self._j = 0

    def at(self, t):
        j = self._j
        if j >= len(self.t) - 1 or self.t[j] > t:
            j = 0
        while j + 1 < len(self.t) and self.t[j + 1] < t:
            j += 1
        if j + 1 >= len(self.t) or self.t[j] > t or self.t[j + 1] - self.t[j] > 1.0:
            return None
        self._j = j
        r = (t - self.t[j]) / (self.t[j + 1] - self.t[j])
        return (self.x[j] + r * (self.x[j + 1] - self.x[j]),
                self.y[j] + r * (self.y[j + 1] - self.y[j]))


def resample(times, values, t0, t1, hz=HZ):
    out, j = [], 0
    for k in range(int((t1 - t0) * hz)):
        t = t0 + k / hz
        while j + 1 < len(times) and times[j + 1] < t:
            j += 1
        if j + 1 >= len(times) or times[j] > t or times[j + 1] - times[j] > 2.0:
            out.append(None)
            continue
        span = times[j + 1] - times[j]
        r = (t - times[j]) / span if span else 0.0
        out.append(values[j] + r * (values[j + 1] - values[j]))
    return out


def correlate(a, b, lag):
    xs, ys = [], []
    for i in range(len(a)):
        j = i + lag
        if 0 <= j < len(b) and a[i] is not None and b[j] is not None:
            xs.append(a[i])
            ys.append(b[j])
    if len(xs) < 200:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else None


@dataclass
class SyncResult:
    session: object
    recording: object
    offset: float          # t_video = t_chrono + offset
    r_speed: float         # correlation des profils de vitesse
    error: float           # ecart de position median, metres
    per_lap: list = field(default_factory=list)   # [(num, offset, ecart)]

    @property
    def spread(self):
        """Dispersion du decalage entre tours : revele une timeline video qui derive."""
        offs = [o for _, o, _ in self.per_lap]
        return max(offs) - min(offs) if len(offs) > 1 else 0.0

    @property
    def usable(self):
        """Calage assez sur pour etre livre."""
        return (self.r_speed >= MIN_R and self.error <= MAX_ERROR
                and self.spread <= MAX_SPREAD)

    @property
    def healthy(self):
        """Calage franc. En dessous, le resultat reste exploitable mais merite
        d'etre signale (timeline video qui derive, GPS GoPro degrade)."""
        return self.r_speed > 0.99 and self.error < 5.0 and self.spread < 0.2

    def diagnosis(self):
        """Ce qui cloche, en clair."""
        out = []
        if self.r_speed < MIN_R:
            out.append(f"correlation des vitesses trop faible ({self.r_speed:.3f} "
                       f"< {MIN_R}) : la video ne semble pas correspondre a cette session")
        if self.error > MAX_ERROR:
            out.append(f"ecart de position de {self.error:.0f} m (> {MAX_ERROR:.0f} m) : "
                       f"trajectoires incompatibles, circuit different ?")
        if self.spread > MAX_SPREAD:
            out.append(f"decalage instable entre tours ({self.spread:.2f} s) : "
                       f"timeline video probablement corrompue")
        return out

    def video_time(self, t_chrono):
        return t_chrono + self.offset


def _anchor_points(session):
    """Points chrono roules, avec le cap local, pour comparer les positions."""
    xy = session.xy
    pts = []
    for i in range(1, len(session.samples) - 1):
        if session.samples[i].speed < MIN_SPEED:
            continue
        hx = xy[i + 1][0] - xy[i - 1][0]
        hy = xy[i + 1][1] - xy[i - 1][1]
        n = math.hypot(hx, hy)
        if n > 1e-6:
            pts.append((session.samples[i].t, xy[i][0], xy[i][1]))
    return pts


def _median_error(pts, traj, offset):
    d = []
    for t, x, y in pts:
        g = traj.at(t + offset)
        if g is not None:
            d.append(math.hypot(g[0] - x, g[1] - y))
    return (median(d), len(d)) if d else (float("inf"), 0)


def match(sessions, recordings, workdir, utc_offset=None):
    """Apparie chaque enregistrement a la session qu'il recouvre le plus.

    Retourne (paires, decalage_horaire). Leve SyncError si rien ne s'apparie :
    c'est le symptome d'un dump melangeant une journee de chrono et une autre
    de video.
    """
    tracks, anchors = {}, []
    for rec in recordings:
        track = recording_track(rec, workdir)
        if not track:
            log.warning("%s : aucun GPS exploitable dans les rushs", rec.name)
            continue
        anchor = gpmf.utc_anchor(track)
        if anchor is None:
            log.warning("%s : pas d'horodatage GPS satellite (GPSU)", rec.name)
            continue
        tracks[rec.index] = (rec, track, anchor)
        anchors.append((anchor, rec.duration))

    if not tracks:
        raise SyncError(
            "aucun rush ne contient de telemetrie GPS exploitable. Verifier que "
            "le GPS de la GoPro etait actif : sans lui, impossible de caler le "
            "chrono sur l'image.")

    dated = [s for s in sessions if s.start_time is not None]
    if not dated:
        raise SyncError(
            "aucun .ra1 n'a un nom horodate (attendu « AAAA-MM-JJ a HHhMM ») : "
            "la date de session ne peut pas etre determinee.")

    if utc_offset is None:
        utc_offset = detect_utc_offset(dated, anchors)
        log.info("decalage horaire deduit des donnees : UTC%+d", utc_offset)

    pairs = []
    for rec, track, anchor in tracks.values():
        best = None
        for s in dated:
            st = naive_epoch(s.start_time) - utc_offset * 3600
            ov = min(anchor + rec.duration, st + s.duration) - max(anchor, st)
            if ov > 0 and (best is None or ov > best[1]):
                best = (s, ov)
        if best is None:
            log.warning("%s : aucune session chrono ne recouvre cette video", rec.name)
            continue
        pairs.append((best[0], rec, track))

    if not pairs:
        day = datetime.fromtimestamp(anchors[0][0], timezone.utc)
        raise SyncError(
            f"aucun rush ne recouvre une session chrono. Les videos sont datees du "
            f"{day:%d/%m/%Y} (heure GPS) alors que les .ra1 couvrent "
            f"{min(s.start_time for s in dated):%d/%m/%Y} a "
            f"{max(s.start_time for s in dated):%d/%m/%Y} : le dump melange "
            f"probablement deux journees.")
    return pairs, utc_offset


def align(session, rec, track) -> SyncResult:
    """Recalage complet d'une session sur la timeline d'un enregistrement."""
    total = rec.duration

    # etape 2 : profil de vitesse, sur toute la plage possible
    gopro_v = resample([s.t for s in track], [s.speed for s in track], 0, total)
    chrono_v = resample([s.t for s in session.samples],
                        [s.speed for s in session.samples], 0, total)
    peak = None
    for lag in range(-int(session.duration * HZ), int(total * HZ), int(HZ)):
        c = correlate(chrono_v, gopro_v, lag)
        if c is not None and (peak is None or c > peak[0]):
            peak = (c, lag)
    if peak is None:
        raise ValueError(f"{session.path.name} : correlation impossible avec {rec.name}")
    for lag in range(peak[1] - 15, peak[1] + 16):
        c = correlate(chrono_v, gopro_v, lag)
        if c is not None and c > peak[0]:
            peak = (c, lag)
    coarse = peak[1] / HZ

    # etape 3 : position, +/-2 s autour
    traj = Trajectory([s.t for s in track], [s.lat for s in track], [s.lon for s in track],
                      session.samples[0].lat, session.samples[0].lon)
    pts = _anchor_points(session)
    best = None
    for i in range(-200, 201):
        d = coarse + i * 0.01
        err, n = _median_error(pts, traj, d)
        if n >= 300 and (best is None or err < best[0]):
            best = (err, d)
    if best is None:
        raise ValueError(f"{session.path.name} : recouvrement insuffisant avec {rec.name}")
    err, offset = best

    res = SyncResult(session, rec, offset, peak[0], err)
    cross, laps = ra1.laps(session)
    for k, lap in enumerate(laps):
        sub = [p for p in pts if cross[k] <= p[0] < cross[k + 1]]
        if len(sub) < 150:
            continue
        b = None
        for i in range(-60, 61):
            d = offset + i * 0.01
            e, n = _median_error(sub, traj, d)
            if n >= 150 and (b is None or e < b[0]):
                b = (e, d)
        if b:
            res.per_lap.append((k + 1, b[1], b[0]))
    return res


def coverage(result):
    """Pour chaque tour : (num, temps, debut_video, fin_video, etat)."""
    cross, laps = ra1.laps(result.session)
    total = result.recording.duration
    out = []
    for k, lap in enumerate(laps):
        t0 = result.video_time(cross[k])
        t1 = result.video_time(cross[k + 1])
        if t1 < 0 or t0 > total:
            state = "hors video"
        elif t0 >= 0 and t1 <= total:
            state = "complet"
        else:
            state = "partiel"
        out.append((k + 1, lap, t0, t1, state))
    return out
