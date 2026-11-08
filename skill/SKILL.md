---
name: opentool-minis
description: Turn a model from a game the user owns and has installed into a printable 28 mm miniature for a 0.2 mm FDM nozzle with the opentool repo (adapters per engine + one Blender 5.2 back end). Use when the user wants to extract, pose, scale, base, make watertight, check or review a game model as a print; when they name a game, unit or model to print; when a game has no adapter yet; and before downloading any reader.
---

# opentool: game model → printable miniature

The tool lives in this repo (`ot.py`, `opentool/`, `adapters/`, `backend/`, `profiles/`). Read `README.md` once.
Every command: `.venv\Scripts\python -I ot.py <command>` from the repo folder. The user's machine settings are
in `opentool.toml` only.

## Hard rules (never bend them)

1. **Legal** ([legal.md](legal.md)): only a game the user owns and has installed; the install is only ever read;
   nothing extracted (meshes, textures, STLs, renders, logs) goes into the repo or anywhere shared; **encrypted or
   DRM-protected assets → stop and tell the user**, no keys, no unpacking protected executables; mod models are
   personal-use only and their credits stay in every report.
2. **Downloads:** never fetch a reader, add-on, wheel or file without showing the user the exact list (file
   name, source URL, size, licence) and getting a clear yes for that list.
3. **Runner caps:** every Blender call and every heavy step goes through the tool (it uses `opentool/runner.py`):
   at most 3 jobs per PC, 16 GB per job, start only with cap + 4 GB free. Never call `blender.exe` yourself.
   `memory cap hit` = the voxel/grid is too fine for the part: fix the input, never raise the cap.
4. **Checks flag, they don't hide:** a failed check stays in the job's flags and on the sheet. Don't "fix" a
   flag by loosening a threshold without the user's OK.
5. **Never overwrite an input**: stages write new files; per-unit fixes go in
   `<workspace>/<game>/units/<group>/<unit>.json` (or a profile's `units/` folder if they should ship).

## Workflow and approval gates

1. `doctor`. Fix anything it reports (Blender 5.2.x, workspace outside the repo, readers, game installs).
2. **Inventory:** `list <game>` then `units <game>`. Report totals per class and anything low-confidence
   (class guesses, default loadouts) → **user OK** before building anything.
3. **Pilot:** one unit per class the user wants: `export`, `adapter-check`, `plan <game> <unit> --poses 1`,
   `run <game>`. Show each review sheet (`output/<group>/<unit>/*_sheet.png`) → **user OK**.
4. **Batch:** plan the rest, `run`, `status --list`; show sheets of flagged jobs first.
5. Posing by hand (a frame the user picks): one pose per round: `plan ... --clip <clip> --frame <n> --changed "<what changed>"`,
   numbered views on the sheet, the user's notes drive the next round.

Before showing any sheet, read `_report.json`: flags, `fixes` (every automatic repair), scale rule and eye line
(an **estimated** eye line is drawn red on the front/side views - say so), base, voxel size.

## When something fails

| symptom | look at | usual fix |
|---|---|---|
| `adapter-check` FAIL | the listed schema/file errors | adapter bug or wrong profile paths |
| rig role missing | ingest warnings | map it in the game profile `rig_map` (canonical roles in `opentool/contract.py`); the stage that needs it is off until then |
| `memory cap hit` in build | `logs/jobs/.../build.log` [stage] lines | coarser `geometry.voxel_mm` for that unit (0.05 → 0.06) or fewer subdivision levels |
| feet float / sink | pose.json `feet`, sheet close-ups | rig ankle/knee roles; `pose.feet.min_embed_mm` |
| thin parts flagged | sheet heat map, qa.json `thin_parts` | `geometry.inflate_objects` for that part (unit file), or accept (user) |
| eye line wrong | sheet red bar | set `scale.eye.eye_line_raw` (model units) or map the `eye` role in the unit file |

## No adapter for this game

Follow [new-engine.md](new-engine.md): fingerprint the engine, check encryption first, find an open reader,
show the download list, pilot one model, write `adapters/<engine>/` from `adapters/_template/`, pass
`adapter-check`, run one job, show the sheet.

## Files a job writes (workspace, never the repo)

`packages/<group>/<unit>/` (package.json + model + textures), `work/<group>/<unit>/` (ingest, scale, poses,
jobs/<loadout>_pose<N>/ with every stage's json), `output/...` (STL, sheet, report), `state/`, `logs/`.
