# opentool: game models → printable 28 mm miniatures

A small Python tool plus a Claude Code skill that turns a model from a game **you own and have installed** into a
printable miniature for a 0.2 mm FDM nozzle: posed from one of the game's own animation frames, scaled (to eye
height for infantry), with its weapons, made watertight and thick enough to print, on a base, checked, and shown
on a review sheet. The design is in [DESIGN.md](DESIGN.md).

Two halves:

- **Adapters** (one per engine) read the game with a third-party reader *you* download and write one standard
  **export package**. v1 ships `unity_assetstudio` (Unity, via AssetStudioModCLI), `relic_essence1`
  (Dawn of War 1 era Relic games, via relic-tool-sga + blender_dow) and `unreal_cue4parse` (UE4/UE5, via a small
  CLI on CUE4Parse; tested on Return to Moria and Aliens: Dark Descent) and `source_sourceio` (Source 1 .mdl
  games, via the SourceIO Blender add-on; tested on Left 4 Dead 2).
- **One shared back end** (Blender 5.2) that only ever sees the package: ingest → scale → pose select → pose →
  weapons → cleanup → relief → solid → thicken → base → checks → review sheet → STL + report.

No adapter for your game? The skill's [new-engine path](skill/new-engine.md) walks Claude through writing one.

**Licence:** GPL-3.0-or-later (see [LICENSE](LICENSE)). **Legal limits:** [skill/legal.md](skill/legal.md). In short:
your own installed game only, the install is only ever read, nothing extracted is ever shared, encrypted games are
out (no keys, no DRM work), mod content is personal-use only with credits kept.

## What you need

| | |
|---|---|
| OS | Windows 10/11 x64 (the memory cap uses a Windows job object; the readers are Windows builds) |
| Blender | **5.2.x** exactly, at the path you give in `opentool.toml`. Always run headless with `--factory-startup`; add-ons are enabled per run from your reader folder, never installed into your Blender |
| Python | 3.11+ (tested on 3.14), with its own venv for this tool |
| RAM | 32 GB minimum (one 16 GB job + 4 GB margin + Windows); 64 GB for 2–3 jobs at once |
| Disk | a workspace folder **outside this repo** with a short path (e.g. `C:/opentool-work`: AssetStudio's .NET build stops at Windows' 260-character path limit): ~0.1–1 GB per unit; a Dawn of War Gold unpack is ~1.8 GB |

## Install

1. Clone this repo. Create the tool's venv and install its four packages (the only Python dependencies):

   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   ```

2. Download the readers for your engine yourself (they are not bundled; check their licences):

   | engine | file | from | licence |
   |---|---|---|---|
   | Unity | `AssetStudioModCLI_net472_win32_64.zip` (v0.19.0, 10,992,121 bytes, sha256 `cad5b9e2084dda85007012f94c6d3ad1944ba153796c4e80027792d0ba7be285`; the version the Battlesector regression was built with) | https://github.com/aelurum/AssetStudioMod/releases/tag/v0.19.0 | MIT |
   | Relic | `blender_dow-0.15.0.zip` (sha256 `7185246722d0246a06940a46b90855f9cf4d9490fa9fcebdef4c0f239c8c49d5`) | https://github.com/amorgun/blender_dow/releases/tag/v0.15 | GPL-3 |
   | Relic | `relic-tool-sga-v2==2.1.0` in its own venv (`pip install relic-tool-sga-v2==2.1.0 "setuptools<81"`; `fs` still needs `pkg_resources`) | PyPI | GPL-3 |
   | Unreal | CUE4Parse + CUE4Parse-Conversion `1.2.2.202610` and their 40 dependencies (pinned in `adapters/unreal_cue4parse/cli/packages.lock.json`), restored by `build_reader.py` | nuget.org | Apache-2.0 (deps MIT / Apache-2.0 / BSD-2; ImageSharp: Six Labors split licence) |
   | Unreal | source for `CUE4Parse-Natives.dll` (ACL animation decompression; most UE4.25+ games): CUE4Parse tag `1.2.2.202610` (its `CUE4Parse-Natives` folder), nfrechette/acl `414689d` (into `CUE4Parse-Natives/ACL/external/acl`), nfrechette/rtm `d7982f2` (into `acl/external/rtm`) | github.com | Apache-2.0, MIT, MIT |
   | Source 1 | `SourceIO.zip` from release 5.5.4 (5,966,664 bytes, sha256 `c6da6d989120b430df0a74315307fc42b8f87fa3c1d98554ee589c7e93f79e4d`; save it as e.g. `SourceIO-5.5.4.zip`) | https://github.com/REDxEYE/SourceIO/releases/tag/5.5.4 | MIT |

   Unzip AssetStudio anywhere (it needs .NET Framework 4.7.2, part of Windows 10/11). The blender_dow and SourceIO
   zips can stay zipped: the tool extracts each once into your workspace (SourceIO's prebuilt `pylib.pyd` is part of
   that release). Source games are read in place from their VPKs, in the order the game's `gameinfo.txt` mounts them;
   nothing is unpacked.

   Unreal: build the reader once with the .NET 10 SDK (and CMake + Visual Studio Build Tools for the ACL part):
   `python -I adapters/unreal_cue4parse/build_reader.py C:/opentool-readers/ue --natives <CUE4Parse-Natives folder>`,
   then set `[readers] ue_cli`. Unreal games pass three gates first (plain reads of the install): **encrypted →
   out**; **Oodle-compressed** → needs the game's own `oo2core_*.dll` or one you already have (`[readers] oodle_dll`;
   most UE4.27+/UE5 games link Oodle into the exe, so there is none in the install); **unversioned properties** →
   needs a `.usmap` mappings file you already have (`usmap` in the game's block). The tool never downloads an Oodle
   DLL and never makes a mappings file.

3. Edit **`opentool.toml`** (the only file you change): Blender path, workspace, reader paths, and one
   `[games.<id>]` block per installed game (`install` = the game folder; `profile` = a file in
   `profiles/games/`). Optional: `[style] profile = "derek"` for one person's taste rules instead of neutral defaults.

4. Add the skill to Claude Code: copy this repo's `skill` folder to `%USERPROFILE%\.claude\skills\opentool-minis\`
   (so that `SKILL.md` sits directly in that folder), then start Claude Code in the repo folder. Claude then
   follows the workflow, the approval gates and the new-engine path; ask it e.g. "make a miniature of
   <unit> from <game>".

   ```
   xcopy /E /I skill "%USERPROFILE%\.claude\skills\opentool-minis"
   ```

5. Check everything: `.venv\Scripts\python -I ot.py doctor`

## Use

```
python -I ot.py list battlesector                 # read the game data once -> units.json (Battlesector: ~1 h first time)
python -I ot.py units battlesector --find Intercessor
python -I ot.py export battlesector UltramarinesIntercessor
python -I ot.py adapter-check battlesector UltramarinesIntercessor
python -I ot.py plan battlesector UltramarinesIntercessor --poses 2      # or --clip idle_01 --frame 297
python -I ot.py run battlesector
python -I ot.py status battlesector --list
```

(`python` = your venv's `.venv\Scripts\python`.) Prints land in `<workspace>/<game>/output/<group>/<unit>/`:
`<unit>_<loadout>_pose<N>.stl`, `_sheet.png` (the review sheet: numbered views at one scale, silhouette,
close-ups, what the checks found) and `_report.json` (provenance, credits, every automatic fix, the checks).
A job whose checks still fail after the build's own repairs is **flagged**, never silently fixed.

Approval gates (the skill enforces them): inventory → your OK → one pilot unit per class → your OK → batch;
one pose at a time when posing by hand.

## Limits that stay on

The runner allows at most **3 heavy jobs per PC** (across every tool that shares `runner.slot_dir`), caps each at
**16 GB committed memory** (hard max 24), and starts a job only with cap + 4 GB free. You can lower these, not
switch them off. A job that hits the cap is stopped by Windows and reported as `memory cap hit`: that means a
voxel or grid size is wrong for the part, so fix the input (the solid stage already picks a voxel that fits the
cap: `geometry.adaptive_voxel`), never raise the cap.

## Regression (Battlesector)

`python -I ot.py regress battlesector` rebuilds the v1 Ultramarines Intercessor (Bolt Rifle, idle_01 frame 297)
and checks volume, size and shells against the metrics shipped in `profiles/games/battlesector.json`. The repo
never ships the reference STL (it is a game derivative); if you built one yourself, pass `--reference <stl>` to
compare surfaces too (p99 / max distance).

## Layout

```
ot.py                    entry point          opentool/   cli, config, jobs (queue), runner (caps), pipeline, plan, sheet, schema/
adapters/<engine>/       readers' wrappers    backend/    defaults.json, heights.py, blender/ (one script per stage)
profiles/printers|styles|games/   printer thresholds, taste rules, per-game facts + shipped unit fixes
skill/                   the Claude Code skill (workflow, gates, new engine, legal)
tests/                   contract tests (no meshes, no game data)
```
