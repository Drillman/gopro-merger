"""Outils de traitement d'une journee de roulage : rushs GoPro + chrono GPS 3DMS.

Modules :
    ra1      lecture des fichiers .ra1 du chrono (source de verite des donnees)
    gpmf     lecture de la telemetrie GPMF des MP4 GoPro (sert uniquement au calage)
    sync     appariement session <-> enregistrement et recalage temporel
    video    decouverte des rushs, concatenation, encodage
    overlay  generation de l'incrustation (sous-titres ASS)
    pipeline orchestration de bout en bout
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump"
GPS = DUMP / "GPS"
OUTPUT = ROOT / "output"

__all__ = ["ROOT", "DUMP", "GPS", "OUTPUT"]
