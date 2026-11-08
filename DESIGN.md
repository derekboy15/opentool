# Open tool: game models → printable 28 mm miniatures (design)

*Draft 1, 2026-11-07. **Approved 2026-11-07**; every decision in §11 is settled. Milestone 1 (back end, Unity, Relic) accepted 2026-11-07; milestone 2 (Unreal) accepted 2026-11-08; milestone 3 (Source, piloted on Left 4 Dead 2) accepted 2026-11-08.*

## 1. What it is, in one paragraph

A Claude Code skill plus a small Python tool that a friend can clone and run on their own PC. It turns a model
from a game **they own and have installed** into a printable miniature on a 0.2 mm FDM nozzle: posed from one of
the game's own animation frames, scaled to eye height, with its weapons, made watertight and thick enough to print,
on a base, checked, and shown on a review sheet. It can't work with literally any game. Each engine needs a
**reader** (an adapter), and encrypted games are out. So the tool is built in two halves: thin **adapters per
engine** that turn game files into one standard export package, and **one shared back end** that only ever sees
that package. When a game has no adapter yet, the skill walks Claude through writing one.

```
 game install (read only)
        │
 ┌──────▼──────────────────────┐   ┌───────────────────────────────────────────────┐
 │ ADAPTER (per engine)        │   │ SHARED BACK END (game-agnostic, Blender 5.2)   │
 │  reader the user downloads  │   │ ingest → scale → pose select → pose → weapons  │
 │  + game profile (optional)  ├──►│ → cleanup → relief → solid → thicken → base    │
 │  list  → units.json         │   │ → checks → review sheet → package (STL/3MF)    │
 │  export→ package/           │   │ (optional) cut plan                            │
 └─────────────────────────────┘   └───────────────────────────────────────────────┘
        ▲ no adapter? the skill's "new engine" path writes one, against the contract
```

## 2. Where the pieces come from

The two pipelines in this folder cover **different halves**. That decides what's reused:

| | Battlesector (the author's shelved `pipeline/`) | Dawn of War 1 (the author's `dow1/tools/`) |
|---|---|---|
| Engine / reader | Unity, AssetStudioModCLI (FBX) | Relic Essence 1 (SGA v2 / `.whm`), relic-tool-sga + amorgun's blender_dow 0.15 |
| Front end | game data tables, Addressables catalog, prefab weapon slots, clips per unit | unpack in `.module` load order, provenance + hash dedupe, default loadout from visibility animations, bind-vs-idle rule, mod credits |
| Back end | **full print chain**: scale, pose select, pose, attach, normal→height, displace, repair, base, voxel remesh, checks, renders, package, regression | none past export; renders, contact sheets, HTML gallery, usability stats |

So the back end is mostly the Battlesector chain with the Battlesector-specific parts lifted out. DoW contributes
front-end patterns (load order, dedupe, visibility-driven loadouts, credits) and the review gallery. DoW has
never been run through print prep. Its first print will be the first test that the back end really is
game-agnostic.

## 3. Adapters (front end)

An adapter is two things:

- **Engine reader wrapper.** Code that calls a third-party reader the user downloaded and turns its output into
  the standard package. One per engine.
- **Game profile (optional).** A JSON file of what the engine can't know: which game-data tables hold units and
  loadouts, bone-name map, clip-name roles, class rules, scale hint. Without a profile the adapter still works.
  It lists every model it finds, and Claude or the user fills the gaps by hand.

| Engine | Reader (user downloads) | Licence | Status |
|---|---|---|---|
| Unity | AssetStudioModCLI (aelurum's AssetStudio fork) | MIT (verify at download) | **v1.** Proven on Battlesector: Animator export with bound clips, weapon FBX split, multi-bundle staging |
| Relic Essence 1 (DoW, WA, DC, Soulstorm, mods) | relic-tool-sga-v2 (unpack) + amorgun's blender_dow | relic-tool: check; blender_dow **GPL-3** | **v1.** Proven: 2,810 models exported, imports headless in Blender 5.2 with PIL stubbed |
| Unreal 4/5 | `otue`, a small CLI on CUE4Parse (Apache-2.0) + CUE4Parse-Natives (ACL) built from source; FModel (GPL-3, GUI) by hand | see left | **M2 (2026-11-08).** Return to Moria + Aliens: Dark Descent. Only **unencrypted** containers; unversioned games only with the user's own mappings file (decision 9); Oodle games only with an Oodle DLL the user has |
| Source 1 (Valve) | SourceIO Blender add-on (MIT) 5.5.4, enabled per run; fallback Crowbar + Blender Source Tools (GPL) not needed | see left | **M3 (2026-11-08).** Left 4 Dead 2. VPKs aren't encrypted and are read in place in the game's own mount order |

Other engines (Frostbite, Creation/Bethesda, Godot, FromSoft, in-house engines) go through the "no adapter yet"
path (§6).

### 3.1 Adapter contract

An adapter offers two commands. The tool checks the output against a JSON schema (`opentool adapter-check`)
before the back end will touch it.

**`list` → `units.json`.** One entry per unit or model:

| field | meaning | Battlesector source | DoW source |
|---|---|---|---|
| `id`, `name`, `group` | stable id, display name, faction/race | RuntimeData unit table | ebps path, race folder |
| `class_hint` | infantry / walker / bike / monster / vehicle / flyer / other, plus `confidence` | keywords, IsLarge, rig bones | heuristic (known to misclass the Carnifex as a vehicle) |
| `models[]` | body variants and squad members | ModelOverrideList, member index | `*_kekoulis` / `_codex` pairs |
| `loadouts[]` | each with `weapons[]`: `{slot, weapon_id, mount}`, where `mount` is `joint:<bone>` **or** `mesh_group:<name>` | weapon slots on prefabs (joint mounts) | mesh groups shown or hidden by `.whe` / visibility clips |
| `default_loadout`, `confidence` | what the game shows | first weapon per slot | `vis_master` rule + fallback (515 low-confidence) |
| `clips[]` | name, frames, fps, `role`: idle / aim / shoot / move / melee / die / other | animator controllers | `.whm` animations |
| `provenance` | archive, path inside it, file hash, all sources | bundle + PathID | SGA + load-order winner + sha |
| `credits` | modellers or mod team, if any | none | Unification sub-mod credits |

**`export <id>` → package folder.**

```
package/
  package.json      contract version, adapter + reader versions, units (game unit → metres hint),
                    up axis, handedness, bind_pose_ok, rig_map, clips, mesh_groups, warnings
  model.glb|.fbx    rig + skinned meshes + clips (glTF preferred; FBX accepted: Unity readers export it)
  weapons/<id>.glb  each weapon with its mount (joint + pivot) if separate
  textures/         files with a role each: diffuse, normal {encoding: rgb|dxt5nm|bc5, green: opengl|directx},
                    mask (DoW keeps team colour in diffuse alpha)
```

`rig_map` maps **canonical bone roles** (pelvis, spine, neck, head, eye/brow, `shoulder/elbow/wrist/hip/knee/ankle/ball.L|R`,
weapon joints) to the game's bone names. This replaces every `SHJnt` name hard-coded in the Battlesector scripts.
A missing role turns a stage off rather than failing silently. No `ankle`, for example, means no foot IK and a
warning on the sheet.

**Mount types.** Covering all four engines needs four:

- `joint:<bone>`: the weapon's pivot sits on a bone (Battlesector).
- `socket:<name>`: a bone plus a stored offset (Unreal).
- `bonemerge`: the weapon has its own copy of the character's bones and snaps to them by name (Source).
- `mesh_group:<name>`: the weapon is part of the body mesh and is shown or hidden (DoW, Source bodygroups).

**Rig presets.** A profile can name a preset instead of listing bones: `ue_mannequin` (pelvis, `thigh_l`,
`calf_l`, `foot_l`, `ball_l`, `upperarm_l`, `lowerarm_l`, `hand_l`, `head`) or `valvebiped`
(`ValveBiped.Bip01_Pelvis`, `_L_Thigh`, `_L_Calf`, `_L_Foot`, `_L_Toe0`, `_L_UpperArm`, `_L_Forearm`, `_L_Hand`,
`_Head1`). Many games on those engines keep the stock rig, so the profile only lists the bones that differ.

### 3.2 Unreal adapter (UE4 and UE5), design

**Fingerprint.** `<Game>/Content/Paks/*.pak`, `*.utoc` + `*.ucas` (IoStore, UE 4.26+ and all UE5) and
`<Game>-Win64-Shipping.exe` (its file version gives the engine version). `WindowsNoEditor` in pak names means UE4,
`Windows` means UE5.

**Encryption gate, before anything else.** Read the pak footer's `bEncryptedIndex` byte and each `.utoc` header's
Encrypted container flag (bit 0x2). Both are plain reads of a few bytes. **Any encrypted container that holds
the wanted assets → stop.** A probe of the Unreal games on the author's PC (2026-11-07):

| game | engine | paks / IoStore | encrypted? |
|---|---|---|---|
| Aliens: Dark Descent | UE4 | 17 / 17 (≈ 40 GB) | no |
| Return to Moria | UE4 | 2 / 2 (15 GB) | no |
| Abiotic Factor | UE5 | 2 / 3 (4.7 GB) | no |
| Expedition 33 | UE5 | 33 / 34 | no |
| Cronos: The New Dawn | UE5 | 2 / 2 (19 GB) | no |
| Triangle Strategy | UE4 | 1 big pak | **yes, out** |
| The Blood of Dawnwalker | UE5 | 1 big pak | **yes, out** |

**Second gate: unversioned properties.** Most IoStore and UE5 games are cooked with "unversioned properties". A
reader then needs a **mappings file** (`.usmap`) to parse anything, meshes included. Mappings are normally made
by injecting a dumper DLL into the running game. That's outside a read-only tool and risky with anti-cheat. So
the adapter **never makes mappings itself**. Whether it may accept a `.usmap` the user already has is decision 9
(§11). Without one, an unversioned game is out, and the adapter says so after a one-package test read.

**Reader.**

- **Primary: a small CLI on CUE4Parse** (Apache-2.0, C# library for UE4 + UE5, pak and IoStore). We'd write about
  200 lines of C#: `list`, `export <path>`. The user builds it locally with the .NET SDK, or downloads our
  release build, which carries no game data. It exports skeletal mesh + skeleton + `AnimSequence`s to glTF or psk/psa, and
  textures to PNG.
- **Fallback for UE4 games: UModel / UE Viewer** (MIT, command line: `-export`, `-game=ue4.xx`, glTF option).
  It's simpler but UE5 support is thin.
- **Manual fallback: FModel** (GPL-3, GUI only), when the user wants to browse and pick a file by hand.
- Newer games compress with **Oodle**. Both readers load the game's own `oo2core_*.dll` from the install (read
  only). The tool never copies or ships it.
- Blender side: glTF imports natively. psk/psa needs an importer add-on (DarklightGames' `io_scene_psk_psa`;
  licence and 5.2 support to check), enabled per run like blender_dow.

**Mapping to the contract.**

| contract field | Unreal source |
|---|---|
| units / models | `SkeletalMesh` assets under the game's character folders (profile gives path globs). Blueprint / DataTable reads for proper unit lists need mappings, so without them the list is mesh-based and `confidence: low` |
| loadouts, mounts | weapon `SkeletalMesh`/`StaticMesh` + **skeleton sockets** (`socket:<name>`, bone + offset, from the Skeleton asset). Which weapon goes with which unit is usually in Blueprints → profile or manual |
| clips + roles | `AnimSequence` assets that use the unit's Skeleton. Roles from names (`Idle`, `Aim`, `Fire`, `Walk`, `Death`) via profile regexes. Montages are skipped in v1 |
| scale | 1 unit = 1 cm, Z-up, left-handed (the importer flips). Eye height from the `head` bone + the mesh |
| rig | `ue_mannequin` preset when the bone names match, else the profile |
| normal maps | BC5 (two channels, Z rebuilt), **DirectX green** (Y flipped vs OpenGL) |
| textures | material instances point at textures. The adapter reads the texture parameter names (`BaseColor`, `Normal`, …) best-effort |

**Risks the pilot must settle.** ACL or other custom animation compression (CUE4Parse supports some), games with
custom engine forks, and huge single containers (Cronos 19 GB: list from the index only, never unpack everything).

**Pilot target.** Return to Moria (UE4, unencrypted, and dwarves and orcs make good minis), or Aliens: Dark
Descent (UE4, marines). Either one tells us whether a UE4 IoStore game needs mappings. Abiotic Factor is the
first UE5 try.

**What the M2 pilot settled (2026-11-08).**

- **Reader:** `otue`, a ~250-line C# CLI on CUE4Parse 1.2.2.202610 (NuGet, 42 pinned packages, `packages.lock.json`),
  built by the user with `build_reader.py`. Commands `ls`, `classes`, `skeleton`, `json`, `export`. UModel was not
  needed. Exports go through **USD** (`.usda`): Blender 5.2 imports it natively and bundles USD's Python API (`pxr`),
  so no psk/psa add-on is needed.
- **ACL animation compression is the norm** (both pilots). Decoding needs `CUE4Parse-Natives.dll`, which NuGet does
  not ship: the user compiles its ACL part from source (CUE4Parse + nfrechette/acl + rtm, all permissive; CMake +
  VS Build Tools). No Oodle part is built.
- **Oodle is linked into the exe** in every Oodle game here (Expedition 33, Cronos): no `oo2core_*.dll` in the
  install, so "load the game's own DLL" rarely works. Gate: Oodle-compressed + no DLL in the install + no
  `[readers] oodle_dll` of the user's own → out with a clear message. Never downloaded or shipped.
- **Gates on the author's PC:** Return to Moria (UE4.27, Zlib, versioned) and Aliens: Dark Descent (UE4.27, uncompressed,
  versioned) read with nothing extra. Abiotic Factor (UE5, uncompressed) is **unversioned** → stops without a
  `.usmap`. Expedition 33 stops on Oodle. The unversioned check samples IoStore package headers
  (`PKG_UnversionedProperties`), a plain read.
- **Clips are authored on the skeleton asset**, whose rest pose can differ from the mesh's (Moria orcs: face,
  helmet and hair joints up to 15 cm). The Blender step retargets the way Unreal plays clips: rotation from the
  clip, translation = mesh rest + (clip − skeleton rest).
- **Bone names may hold `-`** (3ds Max bipeds: `Bip001-Pelvis`), which USD paths reject; the export step writes a
  USD-safe copy and renames the Blender bones back to the game's names.
- **Blender's USD importer puts `metersPerUnit` on the SkelRoot empty**, not on the armature: the export step
  unparents (keeping the world transform) before applying it, else the package comes out in centimetres. Sockets
  become bone-parented empties placed against the **rest** pose (with clips on NLA tracks the bones are posed).
- **Loadouts live in Blueprints** (equipment BP → static mesh, attached at a hand socket). `otue json` dumps any
  Blueprint or data asset, which is how the Moria profile found the Gundabad sword; units come from the profile
  (high confidence), every other skeletal mesh is listed mesh-only (low). Modular characters (Moria orc parts,
  Aliens marines in ~380 pieces) need a parts list in the profile: not in M2.
- **Known advisories:** `dotnet restore` flags SixLabors.ImageSharp 3.1.12 and Microsoft.Bcl.Memory 9.0.0
  (pinned by CUE4Parse). The tool only encodes PNGs from the user's own game files; noted in the README.

### 3.3 Source adapter (Source 1, with a note on Source 2), design

**Fingerprint.** Source 1: `<mod>/gameinfo.txt` + `*_dir.vpk`, models as `.mdl` + `.vvd` + `.vtx` (+ `.phy`).
Source 2: `game/<mod>/gameinfo.gi` + `pak01_dir.vpk`, compiled `.vmdl_c`. They're different formats with
different readers.

**Encryption gate.** Source 1 and 2 VPKs aren't encrypted. A few old games ICE-encrypt script files, never
models. The gate still runs, and reports.

**Reader (Source 1).**

- **Primary: SourceIO** (REDxEYE, MIT), a Blender add-on that imports `.mdl` with skeleton, VTF textures and
  VMT materials. It's the same pattern as blender_dow: run inside Blender through the runner, add-on enabled for
  that run from the tool folder. Its README claims Blender 4.5+, so a 5.2 headless test comes first, as it did for
  blender_dow.
- **Fallback: Crowbar** (decompile `.mdl` → reference SMD + one SMD per sequence + `.qc`; licence and command-line
  mode to check) **+ Blender Source Tools** (GPL, SMD import).
- **VPK unpacking:** the `vpk` Python package (MIT; to check) or SourceIO's own VPK reader. Only the files a unit
  needs are pulled out, into the workspace.

**Mapping to the contract.**

| contract field | Source 1 source |
|---|---|
| units / models | `models/**.mdl` under the profile's character folders (e.g. `models/survivors/`, `models/infected/`) |
| variants | **bodygroups** (`$bodygroup`) and **skins**: same idea as DoW mesh groups → `mesh_group:<name>` |
| weapons, mounts | world models (`w_*.mdl`) attached by **bonemerge** (they carry `ValveBiped` bones) or to a QC `$attachment` (bone + offset) → `bonemerge` or `socket:` |
| clips + roles | sequences, often in **shared animation models** pulled in by `$includemodel` (e.g. a `*_anims.mdl`). The adapter follows those links. Roles come from each sequence's **activity** (`ACT_IDLE`, `ACT_RANGE_ATTACK1`, `ACT_WALK`, `ACT_DIESIMPLE`), which is cleaner than guessing from names |
| scale | 1 unit ≈ 1 inch (a standard human is about 72 units tall), Z-up. Eye height from the `eyes` attachment if present, else the head bone |
| rig | `valvebiped` preset for most human characters |
| normal maps | VTF bump maps, DXT. Green convention varies by game → profile setting, checked on the pilot sheet |
| loadouts | NPC/survivor weapon lists live in game scripts and maps, not the model → profile or manual |

**What the M3 pilot settled (2026-11-08).**

- **Reader:** SourceIO 5.5.4 (MIT, one release zip with a prebuilt `pylib.pyd`, Python ABI3) imports headless in
  Blender 5.2 with `--factory-startup`, enabled for the run from the workspace like blender_dow. Its content
  manager mounts the game from `gameinfo.txt` (L4D2: update, the three DLC folders, left4dead2, hl2) and reads
  every file inside the VPKs: nothing is unpacked. Crowbar and Blender Source Tools were not needed.
- **Listing needs no reader:** `vpk.py` reads the `pak01_dir.vpk` name trees (plain reads, 0.1 s for 321 models);
  one Blender scan then reads the candidate MDL headers for bones, attachments, body groups and sequences.
- **SourceIO's frame-animation decoder is wrong for L4D2.** L4D2 keeps survivor animations in `STUDIO_FRAMEANIM`
  blocks inside `$includemodel` files (`anim_<name>.mdl` + `.ani`). SourceIO reads a block as either constants
  or per-frame data (Valve's format has both) and leaves unflagged channels at zero, so 855 of Nick's ~2,300
  animations failed, all rifle idles among them. The adapter patches that one function at run time
  (`sio.patch_frame_anims`); the add-on stays as downloaded. With it, every clip the profile asks for decodes.
- **Clips = sequences, roles = activities.** A clip is a sequence that is not a delta (layers, aim matrices,
  gestures), plays one animation and has an activity; `ACT_IDLE_RIFLE` -> idle, `ACT_MELEE_*` -> melee, by
  regexes in the profile. Blended sequences (9-way walk/run grids) are left out. A unit's `clip_activity`
  (`_RIFLE`) keeps the ones that hold its weapon.
- **Bonemerge without a new back-end path.** SourceIO builds each bone from the MDL's own matrix, so a Blender
  bone's frame is the game bone's frame. The weapon world model is written in the frame of its
  `ValveBiped.weapon_bone`, and the character carries an empty `bm_ValveBiped.weapon_bone` on its bone of that
  name; the back end mounts the weapon on that empty like a joint (contract: `mount: bonemerge` + the weapon's
  `mount_joint`). Weapon sub-bones (magazine, bolt) follow the weapon bone rigidly.
- **Eye line from the `eyes` attachment**, added as a non-deforming bone `attach_eyes` under the head so the
  back end's `eye` role resolves. Other attachments the profile names become bone-parented empties.
- **Scale:** SourceIO imports at 1 unit = 1/16 ft (0.01905 m); the package is in metres and the scale rule
  (eye height for infantry) decides the print size.

**Source 2 note.** ValveResourceFormat (Source 2 Viewer, MIT; its CLI is to check) decompiles `.vmdl_c` and
exports glTF with skeleton and animations. That's very close to the contract already, so a Source 2 adapter would
be small. It's not in this design's scope.

**Pilot target.** Left 4 Dead 2 (decision 8), installed 2026-11-08: Nick (`survivor_gambler.mdl`) with the M16
(`w_rifle_m16a2.mdl`), Derek's pick.

### 3.4 What the 3–5 days per adapter are spent on

| step | Unreal | Source |
|---|---|---|
| reader pilot: one model in, skeleton + clips + textures right | 1–1.5 d (build CUE4Parse CLI, mappings question) | 0.5–1 d (SourceIO in 5.2 headless) |
| adapter code: `list`, `export`, gates | 1–1.5 d | 1 d (`$includemodel`, bodygroups) |
| profile + rig preset for the pilot game | 0.5 d | 0.5 d |
| back end run, review sheet, fixes | 1 d | 1 d |
| **total** | **3.5–4.5 d** | **3–3.5 d** |

## 4. Shared back end

Every stage reads one stage file and writes the next, plus `.json` metrics and a sheet, and never overwrites its
input (the project's stage contract). Every Blender call goes through the runner with its caps (§8).

| # | Stage | Does | Taken from |
|---|---|---|---|
| 1 | ingest | import the package, convert to mm and Z-up, apply transforms, validate `rig_map`, flag cards and open shells | `prepare.py`, DoW `stage3_export` conventions |
| 2 | plan | unit × loadout × pose → jobs. Merge loadouts that build the same geometry | `plan.py`, `jobs.py` (resumable queue) |
| 3 | scale | **infantry: eye height** (sole to pupil line, e.g. Guardsman 28 mm, per-unit target), others: class rule / one factor, fit-to-base, build-volume check | eye-height rebuild (10-25), `unit_scale.py`, `scalecheck.py` |
| 4 | pose select | sample the unit's clips and score frames (feet planted, no hover, weapon not in body, lean), then pick N distinct ones | `pose_select.py` |
| 5 | pose | freeze a game frame as-is, feet onto the base by leg IK, held grenades to the belt | `pose.py`, `anim.py` |
| 6 | weapons | joint mount by mesh pivot, **or** turn on the loadout's mesh groups (DoW) | `attach.py`; DoW visibility rule |
| 7 | cleanup | drop hidden interior shells, duplicate/degenerate tris; thicken cards (straps, hair, banners) | `prep_*`, game-mesh-cleanup skill |
| 8 | relief | normal map → height (decode layout, Frankot–Chellappa), displace. **Off on skin** | `heights.py`, head-relief rule |
| 9 | solid | voxel remesh to one watertight shell, connect floating bits, drop specks | `build.py` finalize, `pp_lib.watertight` |
| 10 | thicken | auto-thicken to the printer profile: walls ≥ 0.45, pins ≥ 0.8, barrels ≥ 1.0, blades ≥ 1.2 mm, gaps ≥ 0.3 mm or fused | `auto_thicken`, CLAUDE.md thresholds |
| 11 | base | base style (default: 25 mm round, 0.7 mm dome, boots pressed 0.4 mm), GW size map if the user supplies one, none for vehicles, flying socket | `base.py`, base-style rule |
| 12 | checks | shells, non-manifold, thickness heat map, overhangs, contact band 0–0.5 mm (bridged). Failures are **flagged, never silently fixed** | `qa.py`, contact-spec skill |
| 13 | review | numbered views at one scale, silhouette, close-ups of hands/feet/weapon, contact sheet + gallery | `qa.py` renders, DoW `stage4_sheets.py`, review-sheet skill |
| 14 | package | STL (+3MF), report.json with provenance + credits | `stages.py` package |
| 15 | cut plan (optional) | written plan + seam suggestions. User cuts in the slicer | cut-plan skill, `split_parts.py` (test only) |

Approval gates stay as in the Battlesector skill: inventory report → **user OK** → pilot (one unit per class) →
**user OK** → batch. One pose at a time when posing by hand.

## 5. Configuration (what becomes config)

Four layers, merged in order (same deep-merge as `bspipe/config.py`):

1. `opentool.toml` (per PC): Blender path, workspace folder, reader paths, runner caps.
2. `profiles/printers/<printer>.json`: build volume, nozzle, the thresholds in stage 10.
3. `profiles/games/<game>.json`: install path, adapter, rig map, clip roles, class rules, scale rule, loose-prop patterns.
4. `workspace/<game>/units/<unit>.json`: per-unit fixes (as `configs/units/...` today), plus job overrides.

Hard-coded today, to become config:

| Where | What | Goes to |
|---|---|---|
| `bspipe/paths.py` | `GAME = <Steam library>/.../Warhammer 40000 Battlesector`, `StreamingAssets/aa/catalog.bin`, `tools/AssetStudioModCLI/...exe`, `tools/venv`, the Blender install path, all output folders under `pipeline/` | game profile / `opentool.toml` / workspace |
| `run_blender.py` | `BLENDER` path, logs and slot locks under the author's training folder, caps via env vars | `opentool.toml`. Slot folder must stay **one per PC** so caps hold across sessions |
| `bs.py regress` | calls `blender.exe` directly (bypasses the runner caps) | must go through the runner |
| `configs/defaults.json` `rig` | `ROOTSHJnt`, `{s}_Leg_AnkleSHJnt`, `{s}_Arm_WristSHJnt`, … | game profile `rig_map` |
| `blender/pose_select.py`, `prep_candidates.py`, `prep_inventory.py`, `split_parts.py`, `anim.py` | `Arm_ElbowSHJnt`, `Root_SHJnt`, `Brow_AuxSHJnt`, `l_Arm_ShoulderSHJnt`, `weapon1_AuxSHJnt`, SHJnt sorting | canonical roles via `rig_map` |
| `defaults.json` `pose.loose_items` | `*GND_FRG*`, `*GND_KRK*`, `to_bone: ROOTSHJnt` | game profile |
| `defaults.json` `pose_select` | clip names `idle/aim/shoot/…`, exclude `die/knockdown/Teleport/…` | clip `role` from the adapter + profile |
| `scale.json` | factor 1758.838, `helmet_bone: head_TopSHJnt`, Intercessor derivation | scale rule per class + eye targets per unit (workspace) |
| `bspipe/classify.py` | game keyword ids `3`, `20012`, `DefaultMovementMode 3` | game profile class rules |
| `bspipe/heights.py` | assumes Unity DXT5nm / BC5 layout, OpenGL green | `normal.encoding` / `green` in the package |
| `bspipe/gamedata.py`, `gamedump.py`, `catalog.py`, `typetree.py`, `extract.py`, `inventory.py`, `plan.py` loadouts | Battlesector's RuntimeData tables, Addressables 2.x catalog, prefab components | Unity adapter (catalog, typetree) + Battlesector game profile (tables) |
| `baseguide.py`, `configs/base_map.json` | GW Event Companion PDF → base per unit | optional user-supplied map. The PDF is GW's and isn't bundled |
| `defaults.json` `memory` | `budget_gb 56`, `v1_build_gb 31` | replaced by the runner caps (§8) |
| `defaults.json` `faction_priority`, `pose_count_by_base`, `printer` | Derek's run order, pose counts, P2S | workspace prefs / printer profile |
| `dow1/tools/unpack_all.py` | `COMMON = <Steam library>/steamapps/common`, Gold/Soulstorm/Unification source names | game profile (Relic: list of `.module` files) |
| `stage3_run/summary/verify.py`, `stage4_run/sheets.py` | `--root <the author's DoW work folder>`; 8 workers | workspace; workers capped by the runner |
| `stage4_bs_render.py` | `SHELF = <an archive folder>` (stale since a folder move) | drop (one-off comparison) |
| `dow_import.py`, `stage3_export.py` | `dow1/unpacked/<source>`, `dow1/tools/addon_src`, PIL stub, `art/ebps/races/<race>/(troops|structures)`, `vis_master`, banner default rule | Relic adapter + DoW profile |
| `unification_submods.json`, `credits_map.json` | mod credits | DoW profile (data, not code) |

## 6. "No adapter yet" path (in the skill)

1. **Identify the engine** from fingerprints in the install. A table in the skill lists them: `UnityPlayer.dll`
   + `*_Data/` → Unity; `*.pak` / `*.utoc` + `*-Win64-Shipping.exe` → Unreal; `gameinfo.txt` + `*.vpk` → Source;
   `*.sga` + `*.module` → Relic. Report version clues (Unity version string, pak version, SGA version).
2. **Check for encryption or DRM.** If assets are encrypted (AES pak, obfuscated bundles, packed exe needed to
   read them), **stop** and tell the user. No key hunting, no unpacking protected executables.
3. **Find an open reader.** Search for maintained readers that export glTF/FBX/SMD/OBJ with skeleton and
   clips. Note licence, last release, Windows/headless support. Show the user the **exact file list**
   (names, sizes, source URLs) and download only after they approve.
4. **Pilot one model.** Read one unit, render it, compare triangle and bone counts with the reader's own viewer.
5. **Write the adapter** in `adapters/<engine>/` from `adapters/_template/`. Implement `list` and `export` and fill
   a game profile (rig map, clip roles, scale hint). Mark every guessed field with a `confidence`.
6. **Pass `adapter-check`** (schema, files exist, rig roles resolvable, a clip frame poses), then run one job
   through the back end and show the review sheet.

## 7. Legal and licence boundaries

- **The user owns the game, and it's installed locally.** The tool only reads the install folder and refuses
  to write into it (as `assetstudio.run` and `unpack_all.py` already do).
- **Nothing extracted is ever distributed.** No game files, meshes, textures, STLs or renders in the repo.
  That includes our own regression references: `intercessor_v1.stl` is a game derivative, so the repo ships
  only **metrics** (volume, dims, shells, distances). Each user builds their own reference locally.
- **No breaking of encryption or DRM.** Encrypted games aren't supported, full stop. No keys, no key finders.
- **Third-party readers aren't bundled.** The user downloads them: AssetStudioModCLI, relic-tool-sga,
  blender_dow (GPL-3), Crowbar, CUE4Parse. The tool checks versions and hashes it knows and says where to get them.
- **Mod content** (e.g. Unification) has no reuse licence. Personal prints only, credits kept in every
  report, never share or sell files or prints. The skill says so when a model's provenance is a mod.
- **GW material** (Base Size Guide PDF, GW photos used for eye targets) isn't bundled. A user can supply their own copy.
- **Licence for our code.** Recommendation: **GPL-3.0-or-later** for the whole repo. Blender's position is that
  scripts using `bpy` are GPL when distributed, and `whm_stats.py` follows blender_dow's (GPL-3) importer
  layout, so a Relic reader built from it is GPL anyway. GPL keeps it one simple, compatible licence. The
  alternative is MIT for the venv-side tool and adapters, GPL for `backend/blender/` and the Relic adapter.
  That's more permissive but needs care at the boundary. Derek decides (§11).
- **Hygiene.** No logs, state, usernames or absolute paths from anyone's PC in the repo.

## 8. Prerequisites and limits

| Need | Detail |
|---|---|
| OS | Windows 10/11 x64. The runner's memory cap uses a Windows job object, and readers are Windows builds |
| Blender | **5.2.x** exactly (scripts assert `bpy.app.version[:2] == (5, 2)`). Always `-b --factory-startup` via the runner. Add-ons enabled per run from the tool folder, never installed into the user's profile |
| Python | 3.11+ for the tool's own venv (numpy, scipy, Pillow, psutil), run with `-I`. Readers may need .NET (AssetStudio) or their own venv (relic-tool) |
| RAM | **32 GB minimum** (one 16 GB job + the 4 GB free margin + OS), 64 GB for 2–3 jobs in parallel |
| Disk | per game: DoW took ~44 GB unpacked + 45 GB exports for everything; one unit is ~0.1–1 GB. The tool exports on demand, not everything |
| Claude Code | with this skill installed. The 3d-modeling / blender-3d-modeling skills are helpful but not required |

**Runner caps stay on.** At most 3 Blender jobs per PC across all sessions, **16 GB committed per job** (hard
max 24), start only with cap + 4 GB free. The user can lower them but not switch them off. The same cap
applies to any other heavy process (a venv-side voxel or FFT step).

**Known conflict.** The Battlesector build peaked at ~31 GB per infantry job at 0.03 mm voxels, and ~17 GB at
0.04 mm. Neither fits in 16 GB. The back end must stay under the cap: either voxel-remesh body, weapons and base
separately and union them, or use 0.05 mm voxels (still 4× finer than the nozzle), with `adaptive_voxel`
recalibrated to the cap. This changes what "reproduce the Intercessor" can mean (§10, §11).

## 9. Proposed repo layout

```
opentool/                    (this folder; becomes the repo)
  DESIGN.md  README.md  LICENSE
  opentool/                  cli.py, config.py, jobs.py, runner.py (the capped runner), schema/
  adapters/_template/  adapters/unity_assetstudio/  adapters/relic_essence1/
  backend/blender/           one script per stage (§4), shared bpy helpers
  profiles/printers/p2s_0.2.json   profiles/games/battlesector.json   profiles/games/dow1.json
  skill/SKILL.md  skill/new-engine.md  skill/legal.md
  tests/                     contract tests, metric goldens (no meshes)
<workspace>/<game>/          outside the repo: units/, work/, output/, state/, logs/
```

## 10. Effort and first milestone

In Claude session-days (one session working with Derek's review rounds in between):

| Step | Work | Est. |
|---|---|---|
| 0 | skeleton: CLI, layered config, job queue, runner port, package schema + `adapter-check` | 2 d |
| 1 | back end port: stages 1–14 from `bspipe/` + `blender/` with rig roles instead of names, memory-safe solid stage | 5–7 d |
| 2 | Unity adapter: AssetStudio wrapper, catalog/typetree; Battlesector profile with tables, loadouts, clips | 2–3 d |
| 3 | Relic adapter: SGA unpack in load order, blender_dow import → glTF package; DoW profile with visibility loadouts, credits | 2–3 d |
| 4 | milestone runs + fixes | 2–3 d |
| 5 | skill (workflow, gates, new-engine path, legal), README, a clean-PC install test | 1–2 d |
| | **total** | **~14–20 session-days** |

Unreal and Source adapters are in v1 too (decision 5): about 3.5–4.5 and 3–3.5 days (breakdown in §3.4), so the
**v1 total is about 21–28 session-days**, in three milestones:

- **M1:** back end + Unity + Relic (below).
- **M2:** Unreal, piloted on Return to Moria (UE4), then one UE5 try on Abiotic Factor.
- **M3:** Source, piloted on Left 4 Dead 2 (Nick with the M16).

**Milestone 1: back end + Unity + Relic, two known units end to end.**

- **Battlesector: Ultramarines Intercessor, Bolt Rifle, pose 1** (the v1 regression job). The repo's version must
  match `regression/reference/intercessor_v1.stl` (p99 ≤ 0.03 mm, max ≤ 0.15 mm), *or* the agreed tolerance if
  the 16 GB cap forces coarser voxels. Same shells, volume within 0.1%.
- **DoW: base-game Imperial Guard `guardsmen.whm`.** Extraction must match the Stage 1 numbers (21 meshes, 1,898
  default tris, 55 bones, 1.954 units tall). Then the back end makes a watertight print that passes every check
  at 28 mm eye height. There's no earlier print to compare with, so the bar is checks + Derek's look at the sheet.
- **Done when:** both run from a fresh clone with only `opentool.toml` edited, through the runner, with no file
  read from the author's archived pipelines other than the game installs and the downloaded readers.

## 11. Decisions only Derek can make

**Answered 2026-11-07:** (1) GPL-3.0-or-later for everything; (2) public repo; (3) coarser voxels (0.05 mm or
per-part remesh) under the 16 GB cap with a looser bar (e.g. p99 ≤ 0.05 mm); (4) neutral defaults, Derek's rules
ship as a named profile. (5) v1 covers **Unity, Relic, Unreal and Source**; (6) mod models supported,
personal use only, credits kept; (7) fresh reader downloads for the milestone (exact file list approved first);
(8) Source pilot on **Left 4 Dead 2**, which Derek installs; (9) Unreal: accept a `.usmap` the user already has,
the tool never makes one. **All decisions settled.**

1. **Licence:** GPL-3.0-or-later for everything (recommended), or MIT + GPL split?
2. **Sharing:** a private repo just for the friend, or public? Under whose name?
3. **Intercessor bar under the 16 GB cap:** accept 0.05 mm voxels / per-part remesh with a looser tolerance
   (e.g. p99 ≤ 0.05 mm), or keep the 0.03 mm bar and let this one regression job run with a higher cap (up to 24 GB)?
4. **Defaults for other people:** ship your taste rules as defaults (eye height with Guardsman 28 mm, 25 mm
   domed base, no relief on skin, one STL + cut plan with no auto split), or neutral defaults with yours as a profile?
5. **Scope of v1:** Unity + Relic only, with Unreal/Source as "no adapter yet" examples? Or should one of them be in the first release?
6. **Mod content:** support Unification-style mod models (personal use, credits kept), or vanilla game files only?
7. **Readers for the milestone:** reuse the AssetStudioModCLI and blender_dow 0.15 copies already on the author's PC, or
   do a fresh download (exact file list shown first) to test the install path the friend will follow?
8. **Source pilot game:** no complete Source game is installed. Install Left 4 Dead 2, Half-Life 2 or TF2 when
   the Source adapter's turn comes, or leave Source as a designed-but-untested adapter?
9. **Unreal mappings (`.usmap`):** many UE5 games can't be read without one, and making one means injecting a
   dumper into the running game. Accept a `.usmap` the user already has (the tool never makes one), or treat
   every game that needs mappings as out of scope?
