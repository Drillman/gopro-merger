"""Grandeurs derivees du chrono, pour l'incrustation.

Le .ra1 fournit position, vitesse et acceleration longitudinale. Le HUD demande
en plus : l'ecart au tour de reference, l'acceleration laterale, et un trace du
circuit exploitable en 2D. Tout est recalcule ici a partir du chrono, qui reste
la seule source des valeurs affichees.
"""

import math
from dataclasses import dataclass

from . import ra1

G = 9.80665


def cumulative_distance(session):
    """Distance cumulee, en metres, depuis le debut de la session."""
    xy = session.xy
    out = [0.0]
    for i in range(1, len(xy)):
        out.append(out[-1] + math.dist(xy[i - 1], xy[i]))
    return out


def lateral_g(session, window=2):
    """Acceleration laterale, en g, deduite de la vitesse et du taux de virage.

    Le .ra1 ne stocke que le longitudinal ; le lateral se retrouve par
    v * dcap/dt. La fenetre de +/-`window` echantillons (0,2 s par pas) lisse le
    bruit de cap, tres sensible aux 0,4 m de quantification des coordonnees.
    """
    xy = session.xy
    sm = session.samples
    n = len(sm)
    out = [0.0] * n

    def heading(i, j):
        dx, dy = xy[j][0] - xy[i][0], xy[j][1] - xy[i][1]
        return math.atan2(dy, dx) if (dx or dy) else None

    for i in range(window, n - window):
        h0 = heading(i - window, i)
        h1 = heading(i, i + window)
        if h0 is None or h1 is None:
            continue
        dh = (h1 - h0 + math.pi) % (2 * math.pi) - math.pi
        dt = sm[i + window].t - sm[i - window].t
        if dt > 0:
            out[i] = (sm[i].speed / 3.6) * (dh / dt) / G
    return out


@dataclass
class LapWindow:
    """Bornes d'un tour dans le tableau d'echantillons."""
    num: int          # numero affiche (parmi les tours retenus)
    index: int        # index du tour dans la session
    t0: float
    t1: float
    i0: int
    i1: int
    time: float


def lap_windows(session, keep=None):
    cross, laps = ra1.laps(session)
    kept = list(range(len(laps))) if keep is None else list(keep)
    sm = session.samples
    out = []
    for num, k in enumerate(kept, 1):
        t0, t1 = cross[k], cross[k + 1]
        i0 = next((i for i, s in enumerate(sm) if s.t >= t0), 0)
        i1 = next((i for i, s in enumerate(sm) if s.t >= t1), len(sm) - 1)
        out.append(LapWindow(num, k, t0, t1, i0, i1, laps[k]))
    return out


class DeltaModel:
    """Ecart au tour de reference, compare a distance parcourue egale.

    Comparer a distance egale plutot qu'a temps egal est ce qui rend l'ecart
    lisible : il repond a la question << ou en suis-je par rapport au tour de
    reference, a cet endroit du circuit >>.

    La reference est le meilleur tour de la session. C'est un choix de
    post-production : contrairement a un chrono embarque qui ne connait que le
    meilleur tour deja realise, on dispose de toute la session, ce qui donne un
    ecart exploitable des le premier tour.
    """

    def __init__(self, session, windows):
        self.session = session
        self.windows = windows
        self.cum = cumulative_distance(session)
        order = sorted(windows, key=lambda w: w.time)
        self.ref = order[0] if order else None
        # sur le meilleur tour lui-meme, se comparer a soi donnerait un ecart nul
        # de bout en bout : on prend alors le deuxieme meilleur comme reference.
        self._alt = order[1] if len(order) > 1 else None
        self._curves = {}
        for w in (self.ref, self._alt):
            if w is not None:
                self._curves[w.num] = self._curve(w)

    def _curve(self, w):
        """(distance depuis le debut du tour, temps depuis le debut du tour)."""
        sm = self.session.samples
        d0 = self.cum[w.i0]
        return [(self.cum[i] - d0, sm[i].t - w.t0) for i in range(w.i0, w.i1 + 1)]

    def distance_in_lap(self, w, i):
        return self.cum[i] - self.cum[w.i0]

    def reference(self, w):
        """Tour de reference oppose a `w`."""
        return self._alt if w is self.ref else self.ref

    def at(self, w, i):
        """Ecart, en secondes, du tour `w` a l'echantillon `i`. None si N/A."""
        ref = self.reference(w)
        c = self._curves.get(ref.num) if ref else None
        if not c:
            return None
        d = self.distance_in_lap(w, i)
        if d <= c[0][0]:
            return None
        if d >= c[-1][0]:
            ref_t = c[-1][1]
        else:
            lo, hi = 0, len(c) - 1
            while hi - lo > 1:
                mid = (lo + hi) // 2
                if c[mid][0] <= d:
                    lo = mid
                else:
                    hi = mid
            span = c[hi][0] - c[lo][0]
            r = (d - c[lo][0]) / span if span else 0.0
            ref_t = c[lo][1] + r * (c[hi][1] - c[lo][1])
        return (self.session.samples[i].t - w.t0) - ref_t


class TrackMap:
    """Trace du circuit projete dans une boite, avec la position du vehicule.

    Le trace vient du tour de reference reellement roule, pas d'une forme
    generique : la carte est donc celle du circuit du jour.
    """

    def __init__(self, session, window, width, height, padding=0.0):
        self.session = session
        xy = session.xy
        pts = xy[window.i0:window.i1 + 1]
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        span_x = max(x1 - x0, 1e-6)
        span_y = max(y1 - y0, 1e-6)
        inner_w = width - 2 * padding
        inner_h = height - 2 * padding
        self.scale = min(inner_w / span_x, inner_h / span_y)
        # centrage dans la boite ; y inverse (le nord monte a l'ecran)
        self.ox = padding + (inner_w - span_x * self.scale) / 2 - x0 * self.scale
        self.oy = padding + (inner_h - span_y * self.scale) / 2 + y1 * self.scale
        self.points = [self.project(i) for i in range(window.i0, window.i1 + 1)]

    def project(self, i):
        x, y = self.session.xy[i]
        return (x * self.scale + self.ox, -y * self.scale + self.oy)

    def simplified(self, tolerance=1.2):
        """Trace allege : a l'echelle de la carte, un point tous les ~1 px suffit."""
        out = [self.points[0]]
        for p in self.points[1:]:
            if math.dist(p, out[-1]) >= tolerance:
                out.append(p)
        if math.dist(out[-1], out[0]) > tolerance:
            out.append(out[0])
        return out
