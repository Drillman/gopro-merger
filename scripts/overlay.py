"""Incrustation « Track HUD », generee en sous-titres ASS.

Transposition du design Claude Design `Track HUD.dc.html`. Le design est cote a
2704x1520 (resolution native des rushs) ; toutes les valeurs ci-dessous sont
exprimees dans ce repere puis mises a l'echelle par `k = hauteur_sortie / 1520`,
ce qui rend le HUD identique quelle que soit la resolution de rendu.

Pourquoi ASS plutot qu'un rendu navigateur : le poste n'a ni Chrome ni
Playwright, et passer par un moteur HTML imposerait de generer une image par
trame (23 000 PNG pour 6 minutes). libass dessine les panneaux, les vecteurs et
le texte en une seule passe de filtre ffmpeg.

Deux ecarts assumes par rapport au design :

  - le `backdrop-filter: blur()` n'existe pas en ASS. Il est rendu en amont par
    ffmpeg (voir video.build_command) : la zone de chaque panneau est floutee
    avant que le panneau translucide ne soit dessine par-dessus ;
  - les polices du design (JetBrains Mono, Barlow Semi Condensed) ne sont pas
    installees. On utilise Consolas et Bahnschrift, substituts de meme nature
    (mono a chasse tabulaire / lineale condensee). Deposer les .ttf d'origine
    dans `scripts/fonts` et renseigner MONO/SANS suffit a retrouver le design
    exact : ffmpeg les charge via l'option `fontsdir`.

Toutes les valeurs affichees viennent du chrono 3DMS ; le calage GoPro ne sert
qu'a convertir un instant chrono en instant video.
"""

import math

from . import ra1, telemetry

REF_H = 1520.0          # hauteur de reference du design

MONO = "Consolas"       # substitut de JetBrains Mono
SANS = "Bahnschrift"    # substitut de Barlow Semi Condensed
SYMBOL = "Segoe UI Symbol"

AMBER = "FFCC33"
GREEN = "3EE07F"
PURPLE = "C9A4FF"
RED = "FF5A5A"
WHITE = "FFFFFF"
PANEL = "0B0D11"

TICK_TIMER = 0.04       # 25 Hz : le chrono affiche les millisecondes
TICK_DATA = 0.10        # 10 Hz : cadence reelle du chrono GPS

# Plans de superposition ASS : un Layer plus grand est dessine par-dessus. Le
# texte doit donc passer au-dessus des panneaux translucides (plans 1 a 6),
# sinon le fond du panneau le delave — il ressort a 47 % de gris au lieu de blanc.
Z_TEXT = 10

# Libelles du HUD. Le design est en anglais, l'incrustation est en francais.
L_LAP = "TOUR"
L_LAST = "DERNIER"
L_BEST = "MEILLEUR"
L_MAP = "GPS · TRACÉ"
L_LIVE = "DIRECT"
L_SPEED = "VITESSE"
L_GFORCE = "FORCE G"
L_IDLE = "VEILLE"

# Mode veille : hors des tours retenus, le HUD reste affiche mais vide.
V_CLOCK = "-:--.---"
V_LAP = "--"       # numero de tour
V_G = "--"         # valeur de G
V_TIME = "--:--"
V_NUM = "---"

# pied du panneau carte : colonne SPEED | separateur | cadran G + valeur
FOOT_SPLIT = 220        # abscisse du separateur, repere du design
GTEXT_X = FOOT_SPLIT + 20 + 72 + 12


# --------------------------------------------------------------------- helpers

def color(hex_rgb):
    """#RRGGBB du design -> &HBBGGRR& attendu par ASS."""
    return f"&H{hex_rgb[4:6]}{hex_rgb[2:4]}{hex_rgb[0:2]}&"


def style_color(hex_rgb, opacity=1.0):
    """Couleur pour une ligne [V4+ Styles] : &HAABBGGRR, sur 8 chiffres.

    Les surcharges en ligne acceptent la forme courte &HBBGGRR&, pas les styles :
    une valeur tronquee y est relue comme un alpha et delave tout le texte.
    """
    a = max(0, min(255, round(255 * (1 - opacity))))
    return f"&H{a:02X}{hex_rgb[4:6]}{hex_rgb[2:4]}{hex_rgb[0:2]}"


def alpha(opacity):
    """Opacite CSS -> alpha ASS (00 opaque, FF transparent)."""
    return f"&H{max(0, min(255, round(255 * (1 - opacity)))):02X}&"


def ass_time(t):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    return f"{int(h)}:{int(m):02d}:{s:05.2f}"


def lap_clock(t):
    """Chrono du tour : m:ss.mmm, comme le design."""
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m)}:{int(s):02d}.{round((s % 1) * 1000):03d}"


def gap(px):
    """Espacement horizontal dans un run de texte.

    On elargit une espace insecable via \\fsp plutot que via \\fs : changer la
    taille de police ici contaminerait tout le reste de la ligne.
    """
    return f"{{\\fsp{px:.0f}}}\\h{{\\fsp0}}"


# ------------------------------------------------------------------- dessins

def _fmt(v, prec):
    return f"{round(v * (1 << (prec - 1)))}"


def rounded_rect(w, h, r, prec=1):
    """Rectangle a coins arrondis, coin superieur gauche en (0,0)."""
    f = lambda v: _fmt(v, prec)
    return (f"m {f(r)} 0 l {f(w - r)} 0 b {f(w)} 0 {f(w)} 0 {f(w)} {f(r)} "
            f"l {f(w)} {f(h - r)} b {f(w)} {f(h)} {f(w)} {f(h)} {f(w - r)} {f(h)} "
            f"l {f(r)} {f(h)} b 0 {f(h)} 0 {f(h)} 0 {f(h - r)} "
            f"l 0 {f(r)} b 0 0 0 0 {f(r)} 0")


def circle(cx, cy, r, prec=4):
    """Cercle approxime par quatre courbes de Bezier."""
    f = lambda v: _fmt(v, prec)
    c = r * 0.55228
    return (f"m {f(cx - r)} {f(cy)} "
            f"b {f(cx - r)} {f(cy - c)} {f(cx - c)} {f(cy - r)} {f(cx)} {f(cy - r)} "
            f"b {f(cx + c)} {f(cy - r)} {f(cx + r)} {f(cy - c)} {f(cx + r)} {f(cy)} "
            f"b {f(cx + r)} {f(cy + c)} {f(cx + c)} {f(cy + r)} {f(cx)} {f(cy + r)} "
            f"b {f(cx - c)} {f(cy + r)} {f(cx - r)} {f(cy + c)} {f(cx - r)} {f(cy)}")


def polyline(points, prec=4):
    f = lambda v: _fmt(v, prec)
    head = f"m {f(points[0][0])} {f(points[0][1])}"
    return head + " l " + " ".join(f"{f(x)} {f(y)}" for x, y in points[1:])


def segment(x0, y0, x1, y1, prec=4):
    f = lambda v: _fmt(v, prec)
    return f"m {f(x0)} {f(y0)} l {f(x1)} {f(y1)}"


# ---------------------------------------------------------------------- HUD

class Geometry:
    """Position et taille des deux panneaux, dans le repere de sortie.

    Partagee par le rendu ASS et par le calcul des zones a flouter : les deux
    doivent decrire exactement les memes rectangles, sinon le verre depoli
    deborde ou laisse un liseré net.
    """

    def __init__(self, width, height):
        self.w = width
        self.h = height
        self.k = height / REF_H

        # --- panneau chrono (design : top/left 56, padding 22/30)
        # la hauteur de ligne de libass vaut ~1,15 x la taille de police : le
        # rythme vertical est calcule dessus, pas sur la taille nominale
        self.row1_h = self.px(90 * 1.15)
        self.row2_h = self.px(26 * 1.3)
        self.lap_x = self.px(56)
        self.lap_y = self.px(56)
        self.lap_w = self.px(730)
        self.lap_h = self.px(22) * 2 + self.row1_h + self.px(6) + self.row2_h

        # --- panneau carte (design : bottom/right 56, largeur 500)
        self.map_w = self.px(500)
        self.head_h = self.px(68)
        self.map_area_h = self.px(276)
        self.foot_h = self.px(120)
        self.map_h = self.head_h + self.map_area_h + self.foot_h
        self.map_x = width - self.px(56) - self.map_w
        self.map_y = height - self.px(56) - self.map_h

        # zone de trace de la carte (design : padding 18/28, svg 444x252)
        self.plot_x = self.map_x + self.px(28)
        self.plot_y = self.map_y + self.head_h + self.px(18)
        self.plot_w = self.px(444)
        self.plot_h = self.px(252)

    def px(self, v):
        """Valeur du design (repere 1520) -> pixels de sortie."""
        return v * self.k

    def panel_rects(self):
        """Les deux rectangles (x, y, w, h) occupes par le HUD."""
        return [(self.lap_x, self.lap_y, self.lap_w, self.lap_h),
                (self.map_x, self.map_y, self.map_w, self.map_h)]


class Hud(Geometry):
    """Construit le fichier ASS d'une session."""

    def __init__(self, session, offset, width, height, t0=0.0, t1=None, keep=None):
        super().__init__(width, height)
        self.s = session
        self.offset = offset
        self.t0 = t0
        self.t1 = t1
        self.windows = telemetry.lap_windows(session, keep)
        self.delta = telemetry.DeltaModel(session, self.windows)
        self.glat = telemetry.lateral_g(session)
        self.events = []

        ref = self.delta.ref or (self.windows[0] if self.windows else None)
        self.track = (telemetry.TrackMap(session, ref, self.plot_w, self.plot_h,
                                         padding=self.px(14)) if ref else None)

    # ---- utilitaires

    def clip(self, a, b):
        """Fenetre video absolue -> temps relatifs a t0, ou None si hors extrait."""
        if self.t1 is not None and a >= self.t1:
            return None
        if b <= self.t0:
            return None
        end = min(b, self.t1) if self.t1 is not None else b
        return max(a, self.t0) - self.t0, end - self.t0

    def add(self, a, b, style, text, layer=Z_TEXT):
        w = self.clip(a, b)
        if w and w[1] > w[0]:
            self.events.append(
                f"Dialogue: {layer},{ass_time(w[0])},{ass_time(w[1])},{style},,0,0,0,,{text}")

    def span(self):
        """Fenetre couverte par le HUD : toute la video rendue.

        Les panneaux restent affiches de bout en bout ; hors des tours retenus
        ils basculent en veille (voir `standby`).
        """
        if self.t1 is not None:
            return self.t0, self.t1
        if not self.windows:
            return None
        return self.t0, self.windows[-1].t1 + self.offset

    @staticmethod
    def _complement(outer, inner):
        """Ce qui reste de `outer` une fois `inner` retire (inner est contigu).

        Retourne des triplets (debut, fin, apres) ou `apres` distingue le creux
        qui suit `inner` de celui qui le precede.
        """
        a, b = outer
        if inner is None:
            return [(a, b, False)] if b > a else []
        c, d = inner
        out = []
        if c > a:
            out.append((a, c, False))
        if d < b:
            out.append((d, b, True))
        return out

    def idle_ranges(self):
        """Plages ou le CHRONOMETRE est en veille : hors des tours retenus.

        Les tours retenus etant contigus, il n'y a qu'un creux avant et un apres.
        """
        w = self.span()
        if not w:
            return []
        inner = ((self.windows[0].t0 + self.offset,
                  self.windows[-1].t1 + self.offset) if self.windows else None)
        return self._complement(w, inner)

    def data_index_range(self):
        """Premier et dernier echantillon chrono tombant dans la video rendue."""
        lo = hi = None
        for i, s in enumerate(self.s.samples):
            v = s.t + self.offset
            if self.t1 is not None and v > self.t1:
                break
            if v >= self.t0:
                if lo is None:
                    lo = i
                hi = i
        return lo, hi

    def data_span(self):
        """Plage video couverte par les donnees chrono."""
        lo, hi = self.data_index_range()
        if lo is None:
            return None
        sm = self.s.samples
        return sm[lo].t + self.offset, sm[hi].t + self.offset

    def blind_ranges(self):
        """Plages sans aucune donnee chrono : carte, vitesse et G y sont eteints."""
        w = self.span()
        if not w:
            return []
        return [(a, b) for a, b, _ in self._complement(w, self.data_span())]

    # ---- entete ASS

    def header(self):
        k = self.k
        out = [
            "[Script Info]", "ScriptType: v4.00+",
            f"PlayResX: {round(self.w)}", f"PlayResY: {round(self.h)}",
            "WrapStyle: 2", "ScaledBorderAndShadow: yes", "YCbCr Matrix: TV.709", "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
            "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding",
        ]
        # une seule fonte de base : tout est pilote par surcharges en ligne
        for name, font, size in (("Hud", SANS, 26 * k), ("Draw", SANS, 20)):
            out.append(
                f"Style: {name},{font},{round(size)},{style_color(WHITE)},"
                f"{style_color(WHITE)},&H00000000,&H00000000,"
                f"0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1")
        out += ["", "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                "MarginV, Effect, Text"]
        return out

    # ---- panneaux

    def panels(self):
        w = self.span()
        if not w:
            return
        a, b = w
        # chrono
        self.add(a, b, "Draw",
                 f"{{\\an7\\pos({self.lap_x:.1f},{self.lap_y:.1f})"
                 f"\\1c{color(PANEL)}\\1a{alpha(0.55)}"
                 f"\\bord{max(1, round(self.px(1)))}\\3c{color(WHITE)}\\3a{alpha(0.12)}"
                 f"\\shad0\\p1}}{rounded_rect(self.lap_w, self.lap_h, self.px(14))}",
                 layer=1)
        # carte
        self.add(a, b, "Draw",
                 f"{{\\an7\\pos({self.map_x:.1f},{self.map_y:.1f})"
                 f"\\1c{color(PANEL)}\\1a{alpha(0.66)}"
                 f"\\bord{max(1, round(self.px(1)))}\\3c{color(WHITE)}\\3a{alpha(0.14)}"
                 f"\\shad0\\p1}}{rounded_rect(self.map_w, self.map_h, self.px(16))}",
                 layer=1)
        # separateurs horizontaux du panneau carte
        for y in (self.map_y + self.head_h, self.map_y + self.head_h + self.map_area_h):
            self.add(a, b, "Draw",
                     f"{{\\an7\\pos({self.map_x:.1f},{y:.1f})\\1a&HFF&"
                     f"\\bord{max(1, self.px(0.6)):.1f}\\3c{color(WHITE)}"
                     f"\\3a{alpha(0.10)}\\shad0\\p4}}"
                     f"{segment(0, 0, self.map_w, 0)}", layer=1)

    # ---- panneau chrono

    def lap_timer(self):
        x = self.lap_x + self.px(30)
        y1 = self.lap_y + self.px(22)
        y2 = y1 + self.row1_h + self.px(6)

        for w in self.windows:
            v0, v1 = w.t0 + self.offset, w.t1 + self.offset
            sm = self.s.samples

            # ligne 1 : LAP nn | chrono | ecart  (aligne sur la ligne de base)
            i = w.i0
            steps = int(w.time / TICK_TIMER)
            for q in range(steps + 1):
                t = q * TICK_TIMER
                a = v0 + t
                b = min(a + TICK_TIMER, v1)
                while i + 1 <= w.i1 and sm[i + 1].t - w.t0 <= t:
                    i += 1
                d = self.delta.at(w, i)
                if d is None:
                    dtxt = ""
                else:
                    # au tout debut d'un tour l'ecart est nul par construction :
                    # le teinter en rouge donnerait un faux signal
                    if abs(d) < 0.01:
                        arrow, col = "\u25AA", color(WHITE)
                    elif d < 0:
                        arrow, col = "\u25BC", color(GREEN)
                    else:
                        arrow, col = "\u25B2", color(RED)
                    dtxt = (f"{gap(self.px(22))}{{\\fn{SYMBOL}\\fs{self.fs(24)}"
                            f"\\1c{col}}}{arrow}"
                            f"{{\\fn{MONO}\\b1\\fs{self.fs(32)}}}\\h{d:+.3f}")
                self.add(a, b, "Hud",
                         f"{{\\an7\\pos({x:.1f},{y1:.1f})}}"
                         f"{{\\fn{SANS}\\b1\\fs{self.fs(28)}\\1c{color(AMBER)}"
                         f"\\fsp{self.px(4.2):.1f}}}{L_LAP} {w.num:02d}"
                         f"{{\\fsp0}}{gap(self.px(20))}"
                         f"{{\\fn{MONO}\\b1\\fs{self.fs(90)}\\1c{color(WHITE)}}}"
                         f"{self.clock_markup(t)}{dtxt}")

            # ligne 2 : LAST / BEST (ne change qu'au tour)
            last = self.windows[w.num - 2].time if w.num > 1 else None
            best = min((q.time for q in self.windows[:w.num - 1]), default=None)
            self.add(v0, v1, "Hud",
                     f"{{\\an7\\pos({x:.1f},{y2:.1f})}}"
                     f"{{\\fn{SANS}\\b1\\fs{self.fs(26)}\\1c{color(WHITE)}"
                     f"\\1a{alpha(0.55)}\\fsp{self.px(2.1):.1f}}}{L_LAST}{{\\fsp0}}"
                     f"{gap(self.px(10))}"
                     f"{{\\fn{MONO}\\b0\\fs{self.fs(26)}\\1a{alpha(0.9)}}}"
                     f"{ra1.fmt_lap(last) if last else '--:--'}"
                     f"{gap(self.px(32))}"
                     f"{{\\fn{SANS}\\b1\\1a{alpha(0.55)}\\fsp{self.px(2.1):.1f}}}{L_BEST}"
                     f"{{\\fsp0}}{gap(self.px(10))}"
                     f"{{\\fn{MONO}\\b0\\1c{color(PURPLE)}\\1a&H00&}}"
                     f"{ra1.fmt_lap(best) if best else '--:--'}")

    def fs(self, design_size):
        return max(1, round(self.px(design_size)))

    def clock_markup(self, t):
        """m:ss.mmm, le separateur decimal en ambre comme dans le design."""
        txt = lap_clock(t)
        head, _, tail = txt.partition(".")
        return f"{head}{{\\1c{color(AMBER)}}}.{{\\1c{color(WHITE)}}}{tail}"

    # ---- panneau carte

    def map_static(self):
        w = self.span()
        if not w or not self.track:
            return
        a, b = w
        hx = self.map_x + self.px(28)
        hy = self.map_y + self.px(18)
        self.add(a, b, "Hud",
                 f"{{\\an7\\pos({hx:.1f},{hy:.1f})\\fn{SANS}\\b1"
                 f"\\fs{self.fs(26)}\\fsp{self.px(3.6):.1f}}}{L_MAP}")
        # temoin d'etat : il decrit le panneau carte, donc il suit la
        # disponibilite des donnees, pas le decoupage en tours
        lx = self.map_x + self.map_w - self.px(28)
        live = self.data_span()
        for (r0, r1), label, op in (
                ([(live, L_LIVE, 1.0)] if live else [])
                + [(r, L_IDLE, 0.35) for r in self.blind_ranges()]):
            # la pastille est un glyphe du meme run : elle suit ainsi la largeur
            # du libelle, qui change entre DIRECT et VEILLE
            self.add(r0, r1, "Hud",
                     f"{{\\an9\\pos({lx:.1f},{hy:.1f})\\1c{color(AMBER)}"
                     f"\\1a{alpha(op)}}}"
                     f"{{\\fn{SYMBOL}\\fs{self.fs(14)}}}●"
                     f"{{\\fn{SANS}\\b1\\fs{self.fs(22)}"
                     f"\\fsp{self.px(2.6):.1f}}}\\h{label}")

        pts = self.track.simplified(tolerance=self.px(1.4))
        base = (f"\\an7\\pos({self.plot_x:.1f},{self.plot_y:.1f})"
                f"\\1a&HFF&\\shad0\\p4")
        # trace large translucide puis liseré ambre, comme les deux <path> du design
        self.add(a, b, "Draw",
                 f"{{{base}\\bord{self.px(8.3):.1f}\\3c{color(WHITE)}"
                 f"\\3a{alpha(0.20)}}}{polyline(pts)}", layer=2)
        self.add(a, b, "Draw",
                 f"{{{base}\\bord{self.px(2.1):.1f}\\3c{color(AMBER)}"
                 f"\\3a&H00&}}{polyline(pts)}", layer=3)
        # ligne de depart/arrivee, perpendiculaire au trace
        p0, p1 = self.track.points[0], self.track.points[min(6, len(self.track.points) - 1)]
        dx, dy = p1[0] - p0[0], p1[1] - p0[1]
        n = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / n, dx / n
        L = self.px(13)
        self.add(a, b, "Draw",
                 f"{{{base}\\bord{self.px(2.8):.1f}\\3c{color(WHITE)}\\3a&H00&}}"
                 f"{segment(p0[0] - nx * L, p0[1] - ny * L, p0[0] + nx * L, p0[1] + ny * L)}",
                 layer=4)

        # libelles du pied de panneau
        fy = self.map_y + self.head_h + self.map_area_h + self.px(16)
        self.add(a, b, "Hud",
                 f"{{\\an7\\pos({self.map_x + self.px(28):.1f},{fy:.1f})\\fn{SANS}"
                 f"\\b1\\fs{self.fs(22)}\\1a{alpha(0.5)}"
                 f"\\fsp{self.px(3.1):.1f}}}{L_SPEED}")
        self.add(a, b, "Hud",
                 f"{{\\an7\\pos({self.map_x + self.px(GTEXT_X):.1f},{fy:.1f})\\fn{SANS}"
                 f"\\b1\\fs{self.fs(22)}\\1a{alpha(0.5)}"
                 f"\\fsp{self.px(3.1):.1f}}}{L_GFORCE}")
        self.add(a, b, "Draw",
                 f"{{\\an7\\pos({self.map_x + self.px(FOOT_SPLIT):.1f},"
                 f"{self.map_y + self.head_h + self.map_area_h:.1f})\\1a&HFF&"
                 f"\\bord{max(1, self.px(0.6)):.1f}\\3c{color(WHITE)}\\3a{alpha(0.10)}"
                 f"\\shad0\\p4}}{segment(0, 0, 0, self.foot_h)}", layer=1)

        # cadran G : cercle et croix
        cx, cy, r = self.gmeter_center()
        gbase = f"\\an7\\pos({cx:.1f},{cy:.1f})\\shad0\\p4"
        self.add(a, b, "Draw",
                 f"{{{gbase}\\1c{color(WHITE)}\\1a{alpha(0.03)}"
                 f"\\bord{self.px(1.1):.1f}\\3c{color(WHITE)}\\3a{alpha(0.28)}}}"
                 f"{circle(0, 0, r)}", layer=2)
        for seg in (segment(0, -r, 0, r), segment(-r, 0, r, 0)):
            self.add(a, b, "Draw",
                     f"{{{gbase}\\1a&HFF&\\bord{self.px(0.55):.1f}"
                     f"\\3c{color(WHITE)}\\3a{alpha(0.16)}}}{seg}", layer=2)

    def gmeter_center(self):
        cx = self.map_x + self.px(FOOT_SPLIT + 20 + 36)
        cy = self.map_y + self.head_h + self.map_area_h + self.foot_h / 2
        return cx, cy, self.px(33)

    # ---- valeurs dynamiques

    def dynamic(self):
        """Carte, vitesse et G : actifs des que le chrono fournit des points.

        Ces trois blocs ne dependent pas du decoupage en tours : ils suivent la
        disponibilite des donnees, donc ils vivent aussi pendant le tour de
        sortie et celui de rentree, la ou le chronometre reste en veille.
        """
        sm = self.s.samples
        cx, cy, r = self.gmeter_center()
        kg = r / 1.5                      # 1,5 g -> bord du cadran
        fy = self.map_y + self.head_h + self.map_area_h + self.px(16)
        sx = self.map_x + self.px(28)

        lo, hi = self.data_index_range()
        if lo is not None:
            for i in range(lo, hi + 1):
                a = sm[i].t + self.offset
                b = (sm[i + 1].t + self.offset) if i + 1 < len(sm) else a + TICK_DATA
                t = sm[i].t
                s = sm[i]

                # vitesse
                self.add(a, b, "Hud",
                         f"{{\\an7\\pos({sx:.1f},{fy + self.px(26):.1f})}}"
                         f"{{\\fn{MONO}\\b1\\fs{self.fs(62)}}}{s.speed:3.0f}"
                         f"{gap(self.px(6))}{{\\fn{SANS}\\b1\\fs{self.fs(26)}"
                         f"\\1a{alpha(0.55)}}}KM/H")

                # G total et point du cadran
                gl = self.glat[i]
                gt = math.hypot(gl, s.accel)
                self.add(a, b, "Hud",
                         f"{{\\an7\\pos({self.map_x + self.px(GTEXT_X):.1f},"
                         f"{fy + self.px(26):.1f})}}"
                         f"{{\\fn{MONO}\\b1\\fs{self.fs(62)}}}{gt:.1f}"
                         f"{gap(self.px(6))}{{\\fn{SANS}\\b1\\fs{self.fs(26)}"
                         f"\\1c{color(AMBER)}}}G")
                # Convention : le point suit la force RESSENTIE, pas le vecteur
                # acceleration. Virage a gauche -> on est jete a droite, point a
                # droite ; freinage -> on est jete en avant, point vers le haut.
                # Les deux axes doivent suivre la meme convention, sinon le
                # longitudinal parait inverse par rapport au lateral.
                dx = max(-r, min(r, gl * kg))
                dy = max(-r, min(r, s.accel * kg))
                self.add(a, b, "Draw",
                         f"{{\\an7\\pos({cx + dx:.1f},{cy + dy:.1f})\\1c{color(AMBER)}"
                         f"\\1a{alpha(0.22)}\\bord0\\shad0\\p4}}"
                         f"{circle(0, 0, self.px(11))}", layer=3)
                self.add(a, b, "Draw",
                         f"{{\\an7\\pos({cx + dx:.1f},{cy + dy:.1f})\\1c{color(AMBER)}"
                         f"\\1a&H00&\\bord{self.px(1.4):.1f}\\3c{color(PANEL)}"
                         f"\\3a&H00&\\shad0\\p4}}{circle(0, 0, self.px(5.8))}", layer=4)

                # position sur la carte, avec la pulsation du design (1,8 s)
                if self.track:
                    px_, py_ = self.track.project(i)
                    x = self.plot_x + px_
                    y = self.plot_y + py_
                    phase = (t % 1.8) / 1.8
                    if phase < 0.7:
                        f = phase / 0.7
                        self.add(a, b, "Draw",
                                 f"{{\\an7\\pos({x:.1f},{y:.1f})\\1c{color(AMBER)}"
                                 f"\\1a{alpha(0.55 * (1 - f))}\\bord0\\shad0\\p4}}"
                                 f"{circle(0, 0, self.px(12.5) * (1 + 1.6 * f))}", layer=5)
                    self.add(a, b, "Draw",
                             f"{{\\an7\\pos({x:.1f},{y:.1f})\\1c{color(AMBER)}\\1a&H00&"
                             f"\\bord{self.px(2.1):.1f}\\3c{color(PANEL)}\\3a&H00&"
                             f"\\shad0\\p4}}{circle(0, 0, self.px(12.5))}", layer=6)

    # ---- veille

    def standby(self):
        """Valeurs de remplacement, sur deux perimetres distincts.

        Le CHRONOMETRE passe en veille des qu'on sort des tours retenus (tour de
        sortie, tour de rentree, avant et apres la session).

        La CARTE, la VITESSE et le G ne s'eteignent que faute de donnees, c'est
        a dire avant le premier point du .ra1 : des que le chrono enregistre, ils
        sont en direct meme si aucun tour n'est decompte.
        """
        x = self.lap_x + self.px(30)
        y1 = self.lap_y + self.px(22)
        y2 = y1 + self.row1_h + self.px(6)
        fy = self.map_y + self.head_h + self.map_area_h + self.px(16)
        sx = self.map_x + self.px(28)
        gx = self.map_x + self.px(GTEXT_X)
        dim = alpha(0.30)

        # --- chronometre : veille hors des tours retenus
        times = [w.time for w in self.windows]
        for a, b, after in self.idle_ranges():
            self.add(a, b, "Hud",
                     f"{{\\an7\\pos({x:.1f},{y1:.1f})}}"
                     f"{{\\fn{SANS}\\b1\\fs{self.fs(28)}\\1c{color(AMBER)}"
                     f"\\1a{alpha(0.45)}\\fsp{self.px(4.2):.1f}}}{L_LAP} {V_LAP}"
                     f"{{\\fsp0}}{gap(self.px(20))}"
                     f"{{\\fn{MONO}\\b1\\fs{self.fs(90)}\\1c{color(WHITE)}"
                     f"\\1a{dim}}}{V_CLOCK}")
            # apres le dernier tour, le bilan reste lisible : seul le chrono
            # courant s'efface, le dernier tour et le meilleur sont acquis
            if after and times:
                last, best = ra1.fmt_lap(times[-1]), ra1.fmt_lap(min(times))
                op_lbl, op_val, best_col = 0.55, 0.9, color(PURPLE)
            else:
                last = best = V_TIME
                op_lbl, op_val, best_col = 0.35, 0.35, color(WHITE)
            self.add(a, b, "Hud",
                     f"{{\\an7\\pos({x:.1f},{y2:.1f})}}"
                     f"{{\\fn{SANS}\\b1\\fs{self.fs(26)}\\1c{color(WHITE)}"
                     f"\\1a{alpha(op_lbl)}\\fsp{self.px(2.1):.1f}}}{L_LAST}{{\\fsp0}}"
                     f"{gap(self.px(10))}{{\\fn{MONO}\\b0\\1a{alpha(op_val)}}}{last}"
                     f"{gap(self.px(32))}"
                     f"{{\\fn{SANS}\\b1\\1c{color(WHITE)}\\1a{alpha(op_lbl)}"
                     f"\\fsp{self.px(2.1):.1f}}}{L_BEST}{{\\fsp0}}{gap(self.px(10))}"
                     f"{{\\fn{MONO}\\b0\\1c{best_col}\\1a{alpha(op_val)}}}{best}")

        # --- vitesse et G : eteints seulement faute de donnees
        for a, b in self.blind_ranges():
            self.add(a, b, "Hud",
                     f"{{\\an7\\pos({sx:.1f},{fy + self.px(26):.1f})}}"
                     f"{{\\fn{MONO}\\b1\\fs{self.fs(62)}\\1a{dim}}}{V_NUM}"
                     f"{gap(self.px(6))}{{\\fn{SANS}\\b1\\fs{self.fs(26)}"
                     f"\\1a{alpha(0.30)}}}KM/H")
            self.add(a, b, "Hud",
                     f"{{\\an7\\pos({gx:.1f},{fy + self.px(26):.1f})}}"
                     f"{{\\fn{MONO}\\b1\\fs{self.fs(62)}\\1a{dim}}}{V_G}"
                     f"{gap(self.px(6))}{{\\fn{SANS}\\b1\\fs{self.fs(26)}"
                     f"\\1c{color(AMBER)}\\1a{alpha(0.30)}}}G")

    # ---- assemblage

    def build(self):
        self.panels()
        self.map_static()
        self.lap_timer()
        self.dynamic()
        self.standby()
        return "\n".join(self.header() + self.events) + "\n"


def build(session, offset, width, height, t0=0.0, t1=None, keep=None, **_):
    return Hud(session, offset, width, height, t0, t1, keep).build()


def write(session, offset, dest, width, height, **kw):
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build(session, offset, width, height, **kw), encoding="utf-8")
    return dest


def panel_regions(width, height):
    """Zones a flouter en amont (equivalent du backdrop-filter du design).

    Derive de la meme geometrie que le rendu, pour que le flou coincide
    exactement avec les panneaux.
    """
    return [tuple(round(v) for v in r)
            for r in Geometry(width, height).panel_rects()]
