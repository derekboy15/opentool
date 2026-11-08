# A game with no adapter yet

Work through these in order; stop at any step that fails and tell the user why.

## 1. Identify the engine (read-only look at the install)

| fingerprint | engine | version clue |
|---|---|---|
| `UnityPlayer.dll` + `<Game>_Data/` | Unity | `<Game>_Data/globalgamemanagers` header, `UnityPlayer.dll` file version |
| `<Game>/Content/Paks/*.pak`, `*.utoc` + `*.ucas`, `<Game>-Win64-Shipping.exe` | Unreal 4/5 | exe file version; `WindowsNoEditor` in pak names = UE4 |
| `<mod>/gameinfo.txt` + `*_dir.vpk` | Source 1 | `gameinfo.txt` |
| `game/<mod>/gameinfo.gi` + `pak01_dir.vpk` | Source 2 | `.vmdl_c` files |
| `*.sga` + `*.module` | Relic Essence 1 (DoW1 era) | SGA header version (2 = DoW1) |
| `*.sga` + `*.rdo` / `.sgb` | later Relic engines | - |

Anything else (Frostbite, Creation, Godot, FromSoft, in-house): search for the engine name in the install's
files and DLLs, and for community readers.

## 2. Encryption / DRM gate (before anything else)

- Unreal: pak footer `bEncryptedIndex` byte and each `.utoc` header's encrypted flag (0x2). Any encrypted
  container holding the wanted assets → **out**. Unversioned properties need a `.usmap` the user already has;
  the tool never makes one. Oodle compression needs an Oodle DLL the user already has (usually none in the
  install: it is linked into the exe); never download one. The `unreal_cue4parse` adapter runs these gates
  itself (`adapters/unreal_cue4parse/gates.py`): an Unreal game only needs a profile, not a new adapter.
- Source 1: VPKs are not encrypted (a few old games ICE-encrypt script files, never models). The
  `source_sourceio` adapter reads any Source 1 game whose models are MDL v44-v49 (HL2, TF2, L4D/L4D2, Portal 2
  era): a new game only needs a profile (`game_dir`, unit folders, weapons and loadouts, activity roles).
- Unity: bundles with a non-standard signature, or that AssetStudio reports as encrypted/obfuscated → out.
- Anything that needs a key, a patched or unpacked executable, or injecting into the running game → **out**.
Tell the user plainly. No key hunting.

## 3. Find an open reader

Look for maintained readers that export glTF/FBX/SMD/OBJ **with the skeleton and animation clips**, run on
Windows, ideally headless or as a Blender add-on that works in Blender 5.2 with `--factory-startup`. Note for
each: licence, last release, download size. Show the user the **exact file list** (names, sizes, URLs) and
download only after a clear yes. Put downloads in a new empty folder; never run anything from inside an
extracted download folder; run Python that reads game data with `-I`.

## 4. Pilot one model

Read one unit, render it (Blender through the tool's runner, never blender.exe directly), and compare its
triangle and bone counts with the reader's own viewer or log. Settle: units (metres per unit as Blender
imports it), up axis, handedness, bind pose sane, clips present, normal-map encoding and green convention.

## 5. Write the adapter

Copy `adapters/_template/` to `adapters/<engine>/`, implement `list_units(ctx)` and `export_unit(ctx, unit, out)`,
and write a game profile `profiles/games/<game>.json` (`adapter`, `rig_map`, `clip_roles`, scale hint, class
rules, anything the engine can't know). Contract: `opentool/schema/units.schema.json` and
`package.schema.json`; field meanings in DESIGN.md §3.1. Mark every guessed field with a `confidence`.
Mount types: `joint:<bone>`, `socket:<name>`, `bonemerge`, `mesh_group:<name>`.

## 6. Pass adapter-check, run one job, show the sheet

`ot.py export <game> <unit>`, `ot.py adapter-check <game> <unit>` (schema, files, rig roles on the real rig,
a clip frame poses), then `plan --poses 1`, `run`, and show the review sheet with what was guessed.
