"""Lecture de la telemetrie GPMF (flux `gpmd`) des MP4 GoPro.

Le GPMF est un format KLV :

    FourCC(4) | type(1) | taille_struct(1) | repeat(2, big-endian) | payload aligne sur 4

Le type '\\0' designe un conteneur (DEVC, STRM). Le facteur d'echelle SCAL est
propre a chaque STRM : il faut parcourir flux par flux et surtout pas aplatir
le DEVC, sinon on applique le SCAL d'un capteur aux donnees d'un autre.

Un DEVC correspond a ~1 s de video. Deux horloges coexistent :

  - l'index de payload, parfaitement regulier : payload k <-> k * duree/n_payloads ;
  - GPSU, l'UTC issu des satellites, qui jitte de ~270 ms et fait des sauts
    jusqu'a 1 s tant que le recepteur converge (~4 min apres le debut du rush).

On horodate donc les echantillons avec l'index de payload (c'est ce dont
l'incrustation a besoin) et on ne se sert de GPSU que pour l'ancrage absolu,
via une mediane robuste (voir `utc_anchor`).
"""

import struct
from dataclasses import dataclass
from datetime import datetime, timezone

TYPES = {b"b": "b", b"B": "B", b"s": "h", b"S": "H", b"l": "i", b"L": "I",
         b"f": "f", b"d": "d", b"j": "q", b"J": "Q"}
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


@dataclass
class GpsSample:
    t: float          # secondes depuis le debut du rush (index de payload)
    lat: float
    lon: float
    alt: float
    speed: float      # km/h
    fix: int          # 0 aucun, 2 fix 2D, 3 fix 3D
    dop: float        # dilution de precision
    utc: float        # horodatage GPSU du payload, epoch UTC (indicatif)


def children(buf, start, end):
    """Itere les KLV directement contenus dans [start, end) : (fourcc, type, ssize, data)."""
    o = start
    while o + 8 <= end:
        fourcc = buf[o:o + 4]
        t = buf[o + 4:o + 5]
        ssize = buf[o + 5]
        repeat = struct.unpack_from(">H", buf, o + 6)[0]
        size = ssize * repeat
        yield fourcc, t, ssize, buf[o + 8:o + 8 + size]
        o = o + 8 + ((size + 3) & ~3)


def payloads(buf):
    """Decoupe le flux en DEVC successifs : (buf, debut, fin) du contenu."""
    o = 0
    while o + 8 <= len(buf):
        if buf[o:o + 4] != b"DEVC":
            o += 4
            continue
        size = buf[o + 5] * struct.unpack_from(">H", buf, o + 6)[0]
        yield buf, o + 8, o + 8 + size
        o = o + 8 + ((size + 3) & ~3)


def vals(t, ssize, data):
    fmt = TYPES.get(t)
    if not fmt:
        return []
    per = max(1, ssize // struct.calcsize(fmt))
    return [struct.unpack_from(">" + fmt * per, data, i * ssize)
            for i in range(len(data) // ssize)]


def _utc(data):
    s = data.decode("ascii", "ignore").strip("\x00")
    try:
        d = datetime.strptime(s[:12], "%y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (d - EPOCH).total_seconds() + float(s[12:] or 0)


def gps_track(buf, duration):
    """Echantillons GPS d'un rush, horodates sur la timeline video de ce rush."""
    devcs = list(payloads(buf))
    if not devcs:
        return []
    step = duration / len(devcs)
    out = []
    for k, (b, s, e) in enumerate(devcs):
        for fourcc, t, ssize, data in children(b, s, e):
            if fourcc != b"STRM" or t != b"\x00":
                continue
            scal = fix = dop = gps = utc = None
            for fc, ft, fs, fd in children(data, 0, len(data)):
                if fc == b"GPSF":
                    fix = vals(ft, fs, fd)[0][0]
                elif fc == b"GPSP":
                    dop = vals(ft, fs, fd)[0][0] / 100.0
                elif fc == b"GPSU":
                    utc = _utc(fd)
                elif fc == b"SCAL":
                    scal = [v[0] for v in vals(ft, fs, fd)]
                elif fc == b"GPS5":
                    gps = vals(ft, fs, fd)
            if gps is None or not scal:
                continue
            if len(scal) == 1:
                scal = scal * 5
            for i, row in enumerate(gps):
                lat, lon, alt, sp, _ = [row[j] / scal[j] for j in range(5)]
                out.append(GpsSample((k + i / len(gps)) * step, lat, lon, alt,
                                     sp * 3.6, fix or 0, dop, utc or 0.0))
    return out


def clean(samples, min_fix=2, max_dop=5.0):
    """Ecarte les points sans fix exploitable ou a position aberrante."""
    lats = sorted(s.lat for s in samples if s.lat)
    if not lats:
        return []
    med = lats[len(lats) // 2]
    return [s for s in samples
            if s.lat and s.lon and abs(s.lat - med) < 0.5
            and s.fix >= min_fix and (not s.dop or s.dop <= max_dop)]


def utc_anchor(samples, skip=120.0):
    """UTC correspondant a t=0 de la timeline, par mediane de (GPSU - t).

    `skip` ecarte le debut du rush, ou GPSU n'est pas encore stable.
    """
    d = sorted(s.utc - s.t for s in samples if s.utc > 1.7e9 and s.t > skip)
    if not d:
        d = sorted(s.utc - s.t for s in samples if s.utc > 1.7e9)
    return d[len(d) // 2] if d else None
