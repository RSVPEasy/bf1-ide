# bf1 command-line tool

`bf1_cli.py` (in a release, `bf1.exe`) does everything the Level Editor and Mod Loader do, from the command line.

## The interactive shell

Run it with no arguments, or double-click `bf1.exe`, to open a coloured shell:

```bash
python bf1_cli.py
```

- **`open rep.lvl`** picks a level. After that, commands leave the file out: `classes`, `show entc:rep_inf_clone_trooper`, `set entc:rep_inf_clone_trooper MaxHealth=600 -o mod/rep.lvl`. The prompt shows the open file.
- **`help`** lists every command with a short description. `help set` shows every option of one command.
- **Colours:** changes (`old -> new`) are highlighted, written files are green, and errors are red. The up arrow recalls earlier commands.
- **`exit`** quits.

## Scripts and AI agents

With a command, `bf1` runs just that and exits, which is the mode for scripts and AI agents:

- **JSON output:** add `--json` to any command for machine-readable output.
- **Errors:** a failed command prints one line saying what's wrong (`{"error": "..."}` with `--json`) and exits with code 1.
- **Inputs are never changed:** edits are written to `-o/--out`. An existing output file is only overwritten with `--force`.
- **No colour codes when piped:** colours only appear in a real terminal, and never with `--json`.

```bash
python bf1_cli.py --help
```

```bash
python bf1_cli.py set --help
```

## Referring to chunks (REF)

| Form | Example | Meaning |
|---|---|---|
| Index path | `9/34` | Top-level chunk 9 (a nested `lvl_`), then chunk 34 inside it. `ls` prints these. |
| `tag:name` | `entc:rep_inf_clone_trooper` | Searched through every nested level. |
| Name | `rep_inf_clone_trooper` | Works when only one chunk has that name; otherwise the error lists the candidates. |

A level often carries the same texture once per nested level. `export` accepts the name when the copies are identical, and `replace --all` updates every copy.

## Commands

### Looking inside a level

| Command | What it does |
|---|---|
| `info FILE` | Size, nested levels, and chunk counts by tag |
| `ls FILE [--tag T] [--name TEXT] [--top]` | Every chunk with its REF, tag, name and size |
| `show FILE REF` | One chunk in detail: a class's properties, a texture's formats, a model's vertex and segment counts, skeleton, fire points and the classes using it |
| `classes FILE` | Every unit, weapon, ordnance and explosion class (`entc`, `wpnc`, `ordc`, `expc`) with its base and model |

### Editing

| Command | What it does |
|---|---|
| `set FILE REF KEY=VALUE... [--remove KEY...] [--all] -o OUT` | Set, add or remove class properties. `--all` sets every copy of a key that repeats, such as weapon slots. |
| `export FILE REF OUT` | Save a chunk as a usable file: textures as `.png` (or `.dds` if OUT ends in `.dds`), classes as `.odf`, scripts as editable `.lua` (`.luac` for the raw bytecode), animation banks as `.json`, anything else as raw bytes. |
| `replace FILE REF SOURCE [--all] -o OUT` | Replace a chunk from a file of the same kind |
| `hash NAME...` | The engine's hash of each name, to identify an unknown property shown as `0x...` |

### Recipes: many changes across many levels

```bash
python bf1_cli.py apply RECIPE.json FILE.lvl... --out-dir DIR [--force]
```

A recipe is a JSON list of rules. Each rule picks classes and changes their properties:

```json
{
  "name": "Overdrive",
  "rules": [
    { "name": "soldiers: tougher", "tags": ["entc"], "base": ["soldier"], "skip_over": 100000,
      "set": { "MaxHealth": "*1.5", "MaxSpeed": "*1.3" } },
    { "name": "rifles", "tags": ["wpnc"], "classes": ["*_weap_inf_rifle"],
      "set": { "ShotDelay": "*0.75", "Label": "=Battle Rifle" } }
  ]
}
```

| Field | Meaning |
|---|---|
| `tags` | Class kinds to match: `entc` (units, vehicles), `wpnc` (weapons), `ordc` (projectiles), `expc` (explosions) |
| `base` | The class's base, such as `soldier`, `hover`, `flyer`, `walker`, `cannon`, `grenade`, `bolt`, `missile` or `explosion`. `show` prints it. |
| `classes`, `exclude` | Class-name patterns to include or skip, such as `rep_*` or `*_rifle` |
| `set` | Property changes: `"*1.5"` scales, `"+2"` adds, and `"=text"` or plain text sets |
| `add` | `true` also adds `=` properties a class doesn't have |
| `skip_over` | Leave values above this alone, such as heroes' 1000000 health |

**How scaling behaves:**
- **Vectors:** every number in a value like `8.0 14.0` is scaled.
- **Number format:** integers stay integers, and decimals keep their style.
- **Left alone:** values of 0 or less (the game uses them for "none" or "infinite") and anything that isn't purely numbers.

A level often holds the same class once per nested level; every copy is changed. The report shows how many values each rule changed in each file. `recipes/overdrive.json` is a complete example.

[RECIPES.md](RECIPES.md) is a step-by-step tutorial for building one.

### Characters and vehicles

```bash
python bf1_cli.py char FILE MODEL GLB -o OUT [options]
```

This works the same as **Import Character / Vehicle** in the editor, with the same defaults:
- **Size:** the model is fitted to the original's height.
- **Arm angle:** worked out automatically.
- **Texture:** all the `.glb`'s textures are packed into the model's own texture.
- **Budget:** full quality, with big models split into segments the game accepts.
- **Also rebuilt:** the LOD model and collision, and the old shadow is removed.

| Option | Meaning |
|---|---|
| `--list-textures` | Just list the `.glb`'s texture groups (numbers used by `--hair` and `--delete-texture`) |
| `--rotate X,Y,Z` | Turns in degrees, the red, green and blue sliders. `180,0,0` flips an upside-down model. |
| `--axis`, `--size` | Fit along Height, Length, Width or "Largest side", at this size in game units |
| `--arm DEG` | Arm angle below T-pose |
| `--budget N`, `--lod-budget N` | Vertex budgets; default is the `.glb`'s full size |
| `--hair G...`, `--delete-texture G...` | Texture groups that are hair, or to leave out |
| `--texture NAME`, `--keep-texture` | Pack into another texture, or leave textures alone |
| `--no-lod`, `--no-collision`, `--keep-shadow` | Skip those parts |
| `--preview PNG` | Also save front, side, back and test-pose renders of the result |

The output reports any fire points left floating off the new model.

```bash
python bf1_cli.py points FILE MODEL [--move BONE=X,Y,Z...] [--snap] [-o OUT]
```

On its own, `points` lists the model's fire points and hardpoints. `--move` places them at new positions and `--snap` puts floating fire points onto the model; both need `-o`. Soldier LODs get the same moves.

### Scripts

A script (`scr_` chunk) exports as Lua source and goes back in from Lua, compiled to the game's Lua 4.0 bytecode:

```bash
python bf1_cli.py export mission.lvl scr_:bes1a bes1a.lua
```

```bash
python bf1_cli.py replace mission.lvl scr_:bes1a bes1a.lua -o "Mods/Big Battles/mission.lvl"
```

- **Mistakes:** a syntax mistake is reported with its line, and nothing is written.
- **Variable names:** local variables come out as `v1`, `v2` and parameters as `p1`, `p2`, since the game doesn't store their names.
- **Exactness:** every stock script decompiles to code that compiles back to the identical script.
- **Raw bytecode:** end OUT with `.luac` to get the bytecode itself.

### Effects, sky and other settings

Settings chunks export as text in the mod tools' own file syntax and go back in from it:

| Tag | What it is | Exports as |
|---|---|---|
| `fx__` | Effects: a map's fog, water, godrays, colour and weather, and the particle effects in `common.lvl` | `.fx` |
| `sky_` | Sky dome, sun, fog and ambient light | `.sky` |
| `bnd_` | Map boundaries | `.bnd` |
| `prp_` | Foliage scattered over the terrain | `.prp` |
| `load` | Loading screens | `.cfg` |
| `snd_`, `mcfg` | Sound settings (`core.lvl`) and music settings (`shell.lvl`) | `.snd`, `.mcfg` |

```bash
python bf1_cli.py export BES/bes1.lvl sky_:bes1 bes1.sky
```

```bash
python bf1_cli.py replace BES/bes1.lvl sky_:bes1 bes1.sky -o "Mods/Red Sky/bes1.lvl"
```

- **Syntax:** lines look like `FogColor(255, 0, 0);`, and a line can open a `{ ... }` block.
- **Values:** numbers, `"strings"`, and hashes written `0x12345678` or `hash("name")`.
- **Unknown names:** setting names are stored only as hashes. About two-thirds have their real names; the rest show as `0x...` and work as written.
- **Exactness:** every stock settings chunk turns into text and back byte for byte.

### Text

The game's text is in `core.lvl`, one `Locl` chunk per language: `english`, `uk_english`, `french`, `german`, `italian` and `spanish`. Keys are stored only as hashes; 1,101 of the 2,446 have their real names, such as `entity.rep.inf_clone_trooper`, and the rest show as `0x...` hashes, which work anywhere a key does.

| Command | What it does |
|---|---|
| `text ls FILE [LANG] [--find TEXT] [--key PART]` | The languages, or one language's strings |
| `text set FILE LANG KEY=TEXT... [--remove KEY...] -o OUT` | Change, add or remove strings. LANG `all` changes every language. `\n` is a line break. |
| `text export FILE LANG OUT.txt` | One string per line: key, a tab, the text |
| `text import FILE LANG IN.txt -o OUT` | Put an edited export back; listed keys are changed or added |

```bash
python bf1_cli.py text set core.lvl all entity.rep.inf_clone_trooper="SPARTAN" -o Mods/Halo/core.lvl
```

### Movies and sounds

| Command | What it does |
|---|---|
| `media ls FILE` | List the movies in a `.mvs`, or the sounds in a `.bnk` or `core.lvl` |
| `media extract FILE OUTDIR [--only NAME...]` | Extract movies as `.bik` and sounds as `.wav` |
| `media replace FILE SAMPLE=AUDIO.wav... -o OUT` | Replace sounds with your own `.wav` files |

Menu sounds are in `core.lvl`; most other sounds are in `common.bnk`. Real names need `dictionary.txt` next to the tools (see the README); without it, sounds are named by hash, and either form works as SAMPLE.

### Animations

Animations live in animation banks (`zaa_` chunks): the soldiers' in `Common\ENG\common.lvl` (`human_animation_0` to `_4`, `_fp` for first person, `_alert`, `_lowrez`), and walkers', vehicles' and capes' in the side levels next to their models. BANK is the bank's name (the `zaa_:` tag can be left out) or index path.

| Command | What it does |
|---|---|
| `anim ls FILE [BANK]` | Every animation bank, or every animation in one with its frames, length and how many joints move |
| `anim export FILE BANK OUT.json [--only NAME...]` | Save animations as `.json`: per joint, per frame, the rotation (quaternion x, y, z, w) and position |
| `anim import FILE BANK IN.json [--only NAME...] [--as NAME] -o OUT` | Put animations from a `.json` in. A name already in the bank is replaced, a new name is added. |
| `anim edit FILE BANK NAME... [--speed X] [--motion X] [--reverse] [--rename NEW] [--delete] -o OUT` | `--speed 2` is twice as fast, `--motion 1.5` makes movements 50% bigger |
| `anim copy FILE BANK TARGET=SOURCE... [--from OTHER.lvl[::BANK]] -o OUT` | TARGET plays SOURCE's motion and keeps its own name |

- **Names:** the game finds animations by name, which is why `copy` and `--rename` swap them. Names are stored only as hashes; 243 of the 320 stock animations have their real names, such as `human_rifle_stand_runforward`, and the rest show as hashes like `0x9ac27c00`, which work anywhere a name does.
- **Frame rate:** 30 frames a second.
- **Every copy:** a bank found in several nested levels is changed in all of them.

```bash
python bf1_cli.py anim copy common.lvl human_animation_2 human_rifle_stand_walkforward=human_rifle_stand_runforward -o Mods/Hurry/common.lvl
```

### Mod Loader

These use the Mod Loader's saved folders.

| Command | What it does |
|---|---|
| `mods status` | Which mod is installed, and the folders in use |
| `mods ls` | Every mod and its files; the installed one is marked |
| `mods plan MOD` | What installing it would change, without changing anything. Use `original` for the original game. |
| `mods install MOD [--launch]` | Install it, then start the game if `--launch` |
| `mods launch` | Start the game |
| `mods install MOD --launch --console` | Install, start the game, and apply the fake console fix once it's running |
| `mods debugmenu [--force]` | Build the `Debug Menu` mod (Fake Console and Free Camera in the pause menu) from your Original files |
| `randomize [--modes safe wild chaos pure] [--seed N] [--play MODE]` | Roll the randomizer mods into the mods folder; `randomize -h` for every option |

### The running game

These work on the Steam 1.3 `Battlefront.exe` while it's running. Single player only.

| Command | What it does |
|---|---|
| `patch console` | The fake console fix: the pause menu's console commands run instead of crashing |
| `cheats ls` | The cheats in `cheats.txt`, with the built-in ones |
| `cheats run [NAME...]` | Apply cheats until Ctrl+C (default: the ones switched on in the Cheats window) |
| `mem read ADDRESS [--type byte] [--count N]` | Read memory. `+4E6D53` is `Battlefront.exe+4E6D53`; `[+OFFSET]+N` follows a pointer |
| `mem write ADDRESS VALUE --type byte` | Write memory |

### Other tools

| Command | What it does |
|---|---|
| `glbsplit FILE.glb [-n 4] [--merge] [--combine-meshes] [-o OUT]` | Split, merge or combine a `.glb`'s meshes without changing how it looks; `glbsplit -h` for every option |

## Example: a reskin end to end

```bash
python bf1_cli.py char rep.lvl modl:rep_inf_trooper chief.glb -o "Mods/Halo/rep.lvl" --preview chief.png
```

```bash
python bf1_cli.py set "Mods/Halo/rep.lvl" entc:rep_inf_clone_trooper Label="Spartan" -o "Mods/Halo/rep.lvl" --force
```

```bash
python bf1_cli.py mods install Halo --launch
```
