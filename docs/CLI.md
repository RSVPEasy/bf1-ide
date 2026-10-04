# bf1 command-line tool

`bf1_cli.py` (in a release, `bf1.exe`) does everything the Level Editor and Mod Loader do, from the command line. It's built for scripts and AI agents:

- **JSON output:** add `--json` to any command for machine-readable output.
- **Errors:** a failed command prints one line saying what's wrong (`{"error": "..."}` with `--json`) and exits with code 1.
- **Inputs are never changed:** edits are written to `-o/--out`. An existing output file is only overwritten with `--force`.

```bash
python bf1_cli.py --help
```

```bash
python bf1_cli.py char --help
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
| `export FILE REF OUT` | Save a chunk as a usable file: textures as `.png` (or `.dds` if OUT ends in `.dds`), classes as `.odf`, scripts as `.luac`, anything else as raw bytes. |
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

### Randomizer

```bash
python bf1_cli.py randomize FILE.lvl... --out-dir DIR [--seed N] [--min 0.5] [--max 2.0] [--chaos]
```

Every number in every unit, weapon, projectile and explosion class gets a random multiplier between `--min` and `--max`. The seed is printed, so a roll can be replayed with `--seed`.

- **In place:** each number is rewritten with the same number of characters, so the file keeps its exact size and structure. A value can only grow as far as its digits allow; 300.0 tops out at 999.9.
- **Guard rails:** 0/1 flags, "none" or "infinite" values, colours (capped at 255), camera points, physics springs and collision sizes are left alone. `--chaos` randomizes all of it.

**Randomizer mods in the Mod Loader:** a mod folder containing `randomizer.json` is re-rolled from the Original files on every Play, and `mods install` does the same.

```json
{ "files": ["SIDE/*.lvl"], "min": 0.5, "max": 2.0, "chaos": false, "seed": null }
```

`files` are patterns under `Data\_LVL_PC`. A fixed `seed` replays the same game every time. The last roll's seed is saved in `randomizer_last.json` in the mod folder.

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

### Movies and sounds

| Command | What it does |
|---|---|
| `media ls FILE` | List the movies in a `.mvs`, or the sounds in a `.bnk` or `core.lvl` |
| `media extract FILE OUTDIR [--only NAME...] [--mp4]` | Extract movies as `.bik` (or `.mp4` with ffmpeg) and sounds as `.wav` |
| `media replace FILE SAMPLE=AUDIO... -o OUT` | Replace sounds with `.wav` files, or mp3/ogg/flac with ffmpeg |

Menu sounds are in `core.lvl`; most other sounds are in `common.bnk`. Real names need `dictionary.txt` next to the tools (see the README); without it, sounds are named by hash, and either form works as SAMPLE.

### Mod Loader

These use the Mod Loader's saved folders.

| Command | What it does |
|---|---|
| `mods status` | Which mod is installed, and the folders in use |
| `mods ls` | Every mod and its files; the installed one is marked |
| `mods plan MOD` | What installing it would change, without changing anything. Use `original` for the original game. |
| `mods install MOD [--launch]` | Install it, then start the game if `--launch` |
| `mods launch` | Start the game |

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
