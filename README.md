# gopro-merger

Traitement automatique des rushs GoPro sous Windows : regroupement des chapitres d'un même enregistrement, concat sans perte, encodage NVENC H.265 en une passe.

Cible : sorties piste moto, ~6 enregistrements de 20 min par jour. Le fichier de sortie sert de source de re-montage temporaire, à conserver le temps du montage manuel dans DaVinci Resolve.

## Pré-requis

- Windows 10 / 11
- GPU NVIDIA compatible NVENC HEVC (RTX série 20+, GTX 16+, etc.)
- Python 3.10+ — `archive.py` n'utilise que la stdlib ; le paquet `scripts`
  demande `openpyxl` (export xlsx du chrono) : `pip install openpyxl`
- FFmpeg + ffprobe dans le PATH, compilés avec `hevc_nvenc` et `libass`

Installation express :

```powershell
winget install --id Python.Python.3.12 -e
winget install --id Gyan.FFmpeg -e
```

Puis relancer le shell et vérifier que NVENC HEVC est bien présent :

```powershell
ffmpeg -hide_banner -encoders | Select-String hevc_nvenc
```

## Utilisation

Déposer les rushs `.MP4` **et** les fichiers du chrono 3DMS `.ra1` dans
`dump/`, côte à côte, puis **double-cliquer sur `roulage.cmd`** — c'est le seul point
d'entrée. Un menu propose :

| | Action | Sortie |
|---|---|---|
| 1 | Analyse chrono seule | `output/AAAA-MM-JJ.xlsx` |
| 2 | Aperçu vidéo, session la plus courte (960 px) | `output/AAAA-MM-JJ - session N.mp4` |
| 3 | Rendu final, toutes les sessions, natif HEVC NVENC | `output/AAAA-MM-JJ - session N.mp4` |
| 4 | Archivage brut, sans incrustation | `output/recording_XXXX.mp4` |

Les sessions sont numérotées dans l'ordre chronologique de la journée.

`output/` ne contient que des livrables. Les fichiers intermédiaires (listes de
concat, sous-titres ASS, cache de télémétrie GoPro) vivent dans `work/`, à la
racine du projet — supprimables à tout moment, ils sont régénérés au besoin.

En ligne de commande, les mêmes actions sont des sous-commandes et acceptent
toutes les options des outils sous-jacents :

```powershell
roulage.cmd chrono --secteurs 4
roulage.cmd apercu --session 15h53 --tour 2
roulage.cmd rendu  --toutes
roulage.cmd brut   <dossier_input> [dossier_output]
```

L'archivage brut nomme ses sorties `recording_XXXX.mp4` (l'index à 4 chiffres est
celui du nom GoPro `GX01XXXX.MP4`).

## Comment ça marche

1. Scan récursif du dossier d'entrée pour trouver les fichiers `G[XH]CCFFFF.MP4`.
2. Groupement des chapitres par les 4 chiffres finaux `FFFF` (l'index d'enregistrement GoPro — deux fichiers avec le même `FFFF` = même prise, découpée en chapitres).
3. Pour chaque enregistrement, concat des chapitres via le démultiplexeur `concat` de FFmpeg, en alimentant directement l'encodeur — aucun fichier intermédiaire sur disque.
4. Encodage HEVC via `hevc_nvenc` (`-preset p6 -rc vbr -cq 24`, `-tag:v hvc1` pour la lecture cross-platform), audio copié tel quel, streams télémétrie GoPro (GPMF, timecode) écartés via `-map`.
5. Sortie idempotente : si `recording_XXXX.mp4` existe déjà, l'enregistrement est skippé.

Le groupement se fait **sur le nom de fichier**, pas sur le timecode : la GoPro utilisée perd la date au changement de batterie, donc `creation_time` n'est pas fiable. L'index dans le nom, lui, reste toujours cohérent.

## Configuration

Constantes en tête de `scripts/archive.py` :

| Variable  | Défaut     | Rôle                                                        |
|-----------|------------|-------------------------------------------------------------|
| `INPUT`   | `./dump`   | Dossier d'entrée                                            |
| `OUTPUT`  | `./output` | Dossier de sortie                                           |
| `CQ`      | `24`       | Qualité NVENC (plus bas = meilleur, plus lourd)             |
| `BAR_LEN` | `30`       | Largeur de la barre de progression                          |

## Sortie console

```
11:58:39 INFO    demarrage : INPUT=... OUTPUT=... CQ=24
11:58:39 INFO    scan de G:\GoPro dump\dump ...
11:58:39 INFO    6 enregistrement(s) detecte(s) sur 16 chapitre(s)
11:58:39 INFO      - recording_0627 : 3 chapitre(s)
...
11:58:39 INFO    [1/6] recording_0627.mp4 : 3 chapitre(s), ~17m51s -> encode
11:58:39 INFO        chapitre 1/3 : GH010627.MP4
    [1/6] [########------------------------]  27%  5m01s/17m51s   1.4x  ETA   9m10s
12:11:34 INFO    [1/6] recording_0627.mp4 : OK en 12m54s, 1.4x realtime (2580 MB)
...
12:XX:XX INFO    termine : 6 ok, 0 echec(s) - duree totale XX pour XX de rush, moyenne 1.4x realtime
```

La barre de progression est redessinée en place ; en mode non-TTY (sortie redirigée vers un fichier), fallback sur des logs `xx%` tous les 10%.

## Cas limites gérés

- Fichier `.MP4` au nom non-GoPro : warning + skip.
- Deux fichiers avec le même `(index, chapitre)` — collision entre deux cartes SD dont les plages se chevauchent : warning, on garde le premier vu.
- FFmpeg qui plante sur un enregistrement : l'archive partielle est supprimée, la boucle continue avec le suivant, exit `1` en fin de run.
- Ctrl+C : FFmpeg terminé, barre nettoyée, propagation.
- ffmpeg ou ffprobe absent du PATH : exit `2` avec message clair.
- Fichier `.concat_*.txt` toujours nettoyé (bloc `finally`), même en cas de crash.

## Codes de sortie

| Code | Sens                                             |
|------|--------------------------------------------------|
| `0`  | Tout OK (ou tout skippé)                         |
| `1`  | Au moins un enregistrement a échoué              |
| `2`  | Problème de setup (outil manquant, dossier KO)   |

## Organisation

```
roulage.cmd        <- le seul script à cliquer
README.md
scripts/           <- tout le code
dump/              <- entrées : rushs GoPro (.MP4) et chrono 3DMS (.ra1)
output/            <- livrables : vidéos traitées et classeurs xlsx
work/              <- fichiers intermédiaires, non suivis par git
```

| Module | Rôle |
|---|---|
| `__main__` | menu et aiguillage des sous-commandes |
| `archive` | archivage brut des rushs, sans incrustation |
| `ra1` | lecture des `.ra1` du 3DMS (format rétro-conçu), tours, secteurs, export xlsx |
| `gpmf` | lecture de la télémétrie GPMF des MP4 GoPro — sert **uniquement** au calage |
| `sync` | appariement session ↔ rush et recalage temporel |
| `telemetry` | grandeurs dérivées : écart au tour de référence, G latéral, tracé du circuit |
| `overlay` | génération du HUD en sous-titres ASS |
| `video` | découverte des rushs, concat, profils d'encodage |
| `pipeline` | orchestration de bout en bout |

```powershell
ra1.cmd                       # analyse des .ra1, un classeur xlsx par journée
poc.cmd                       # aperçu rapide 960px de la session la plus courte
poc.cmd --profil archive      # résolution native, HEVC NVENC CQ 24
poc.cmd --session 15h53 --tour 2
```

Le chrono est la **source de vérité** des valeurs affichées ; le GPS de la GoPro
ne sert qu'à situer un instant chrono sur la timeline vidéo. Le calage se fait
en trois étapes (UTC satellite → corrélation des profils de vitesse → écart de
position), ce qui donne ~0,02 s de stabilité entre tours.

Un dump ne doit contenir **qu'un seul circuit et une seule journée** : le pipeline
le vérifie et refuse sinon. Le décalage horaire des noms de `.ra1` est déduit des
données, pas figé (`--utc` pour forcer).

## Roadmap

- Suite pipeline : montage Resolve → rendu final NVENC → upload YouTube non-répertorié via API v3.
