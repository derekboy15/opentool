# Legal and licence boundaries

- **The user owns the game, and it is installed locally.** The tool only reads the install folder and refuses
  to write into it (the Unity reader wrapper checks every output folder; the Relic unpacker records each
  archive's size and modification time before and after and fails if anything changed).
- **Nothing extracted is ever distributed.** No game files, meshes, textures, STLs, renders or logs in the repo
  or in anything shared. That includes regression references: the repo ships only **metrics** (volume, size,
  shells); each user builds their own reference locally.
- **No breaking of encryption or DRM.** Encrypted games are not supported, full stop. No keys, no key finders,
  no unpacking protected executables, no injecting into a running game.
- **Third-party readers are not bundled.** The user downloads them (AssetStudioModCLI - MIT; relic-tool-sga -
  GPL-3; blender_dow - GPL-3; CUE4Parse - Apache-2.0, built locally into `otue`; later SourceIO - MIT, ...).
  Proprietary pieces are never fetched by the tool: no Oodle DLL (only one already in the install or on the
  user's PC) and no mappings file (only the user's own `.usmap`). The tool says where to get
  them and which versions it was tested with.
- **Mod content** (e.g. Unification for Dawn of War) has no reuse licence: personal prints only, credits kept in
  every report (`credits`, `personal_use_only`), never share or sell the files or the prints. Say so whenever a
  model's provenance is a mod.
- **Games Workshop material** (the Base Size Guide PDF, GW photos used for eye-height targets) is not bundled. A
  user can supply their own copy.
- **This code:** GPL-3.0-or-later. Scripts using Blender's `bpy` are GPL when distributed, and the Relic reader
  follows blender_dow's (GPL-3) importer layout.
- **Hygiene:** no logs, state, usernames or absolute paths from anyone's PC in the repo. The workspace lives
  outside the repo (the tool refuses a workspace inside it), and `.gitignore` blocks meshes, images and logs.
