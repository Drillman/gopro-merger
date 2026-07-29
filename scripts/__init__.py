"""Outils de traitement d'une journee de roulage : rushs GoPro + chrono GPS 3DMS.

Modules :
    ra1      lecture des fichiers .ra1 du chrono (source de verite des donnees)
    gpmf     lecture de la telemetrie GPMF des MP4 GoPro (sert uniquement au calage)
    sync     appariement session <-> enregistrement et recalage temporel
    video    decouverte des rushs, concatenation, encodage
    overlay  generation de l'incrustation (sous-titres ASS)
    archive  archivage des rushs sans incrustation
    pipeline orchestration de bout en bout

Arborescence :
    dump/    rushs .MP4 et fichiers chrono .ra1, cote a cote
    output/  livrables seulement : videos traitees et classeurs d'analyse
    work/    fichiers temporaires, tenus hors de output/ pour garder le suivi clair
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DUMP = ROOT / "dump"
OUTPUT = ROOT / "output"
WORK = ROOT / "work"

__all__ = ["ROOT", "DUMP", "OUTPUT", "WORK"]
