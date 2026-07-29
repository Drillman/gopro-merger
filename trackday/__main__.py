"""Point d'entree unique du projet.

Sans argument : menu interactif, pour un lancement au double-clic depuis
`roulage.cmd`. Avec arguments : sous-commande directe, les options restantes
etant passees telles quelles a l'outil concerne.

    python -m trackday chrono  [--secteurs N] [--points]
    python -m trackday apercu  [--session 15h53] [--tour 2] ...
    python -m trackday rendu   [--session 15h53] ...
    python -m trackday brut    [dossier_entree] [dossier_sortie]
"""

import logging
import sys

from . import DUMP, GPS, OUTPUT

COMMANDS = {
    "chrono": "analyse des .ra1 seuls, un classeur xlsx par journee",
    "apercu": "video avec incrustation, 960 px, encodage rapide",
    "rendu":  "video avec incrustation, resolution native, HEVC NVENC",
    "brut":   "archivage des rushs sans incrustation",
}


def setup_logging():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")


def run(command, args):
    from . import archive, pipeline, ra1

    if command == "chrono":
        return ra1.main(args) or 0
    if command == "brut":
        return archive.main(args)
    if command == "apercu":
        return pipeline.main(["--profil", "preview", *args])
    if command == "rendu":
        return pipeline.main(["--profil", "archive", *args])
    print(f"commande inconnue : {command}")
    return 2


# --------------------------------------------------------------------- menu

MENU = [
    ("1", "Analyse chrono seule", "chrono", []),
    ("2", "Apercu video - session la plus courte", "apercu", []),
    ("3", "Rendu final - toutes les sessions", "rendu", ["--toutes"]),
    ("4", "Archivage brut, sans incrustation", "brut", []),
]


def inventory():
    ra1_files = sorted(GPS.glob("*.ra1")) if GPS.is_dir() else []
    rushs = sorted(DUMP.glob("*.MP4")) if DUMP.is_dir() else []
    return ra1_files, rushs


def menu():
    ra1_files, rushs = inventory()
    print()
    print("=" * 58)
    print("  Journee de roulage")
    print("=" * 58)
    print(f"  chrono : {len(ra1_files)} session(s)      dans {GPS}")
    print(f"  rushs  : {len(rushs)} fichier(s) GoPro dans {DUMP}")
    print(f"  sortie : {OUTPUT}")
    print()
    if not ra1_files:
        print("  ! aucun .ra1 : deposer les fichiers du chrono dans dump/GPS")
    if not rushs:
        print("  ! aucun .MP4 : deposer les rushs GoPro dans dump/")
    print()
    for key, label, cmd, _ in MENU:
        print(f"  {key}) {label}")
        print(f"     {COMMANDS[cmd]}")
    print("  Q) Quitter")
    print()

    while True:
        choice = input("  Choix : ").strip().lower()
        if choice in ("q", "quit", ""):
            return 0
        for key, _, cmd, extra in MENU:
            if choice == key:
                print()
                return run(cmd, extra)
        print("  choix invalide")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    setup_logging()
    if not argv:
        return menu()
    if argv[0] in ("-h", "--help"):
        print(__doc__)
        print("Sous-commandes :")
        for name, desc in COMMANDS.items():
            print(f"  {name:8s} {desc}")
        return 0
    return run(argv[0], argv[1:])


if __name__ == "__main__":
    sys.exit(main())
