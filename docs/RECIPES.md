# Building a recipe

A recipe is a small text file that lists changes to the game's classes, such as "every sniper rifle fires twice as fast" or "every soldier has 25% more health". The `apply` command makes those changes across as many levels as you like, writes the changed levels into a mod folder, and never touches the originals. One recipe can rebalance the whole game.

This tutorial builds a complete recipe, **Sniper Duel**, one step at a time. The finished file is [recipes/sniper-duel.json](../recipes/sniper-duel.json).

## What you need

- **The command-line tool:** `bf1.exe` from the release, or `python bf1_cli.py` when running from source.
- **A text editor:** Notepad works. VS Code and Notepad++ are nicer, because they colour JSON and point at mistakes.
- **Clean copies of the game's levels:** use the untouched originals folder you gave the Mod Loader, not `GameData\Data\_LVL_PC` while another mod is installed. Otherwise your recipe stacks on top of that mod.

To run `bf1`, open a Command Prompt in the folder that holds `bf1.exe`: type `cmd` in File Explorer's address bar and press Enter. The examples below use `Originals\SIDE\rep.lvl`; swap in your own path, keeping quotes around paths that contain spaces.

## How the game's classes work

Everything in a battle is a **class** with a list of **properties**:

| Tag | What it is | Example class | Example properties |
|---|---|---|---|
| `entc` | Units and vehicles | `rep_inf_clone_trooper` | `MaxHealth`, `MaxSpeed`, `WeaponName1` |
| `wpnc` | Weapons | `rep_weap_inf_sniperrifle` | `ShotDelay`, `ReloadTime`, `RoundsPerClip` |
| `ordc` | Ordnance: what a weapon fires | `rep_weap_inf_sniperrifle_ord` | `MaxDamage`, `Velocity` |
| `expc` | Explosions | `rep_weap_inf_thermaldetonator_exp` | `DamageRadius`, `Push` |

Each class also has a **base**, its general type: `soldier`, `hover`, `flyer` or `walker` for units; `cannon`, `grenade` or `launcher` for weapons; `bolt`, `missile` or `sticky` for ordnance; `explosion` for explosions.

A rule in a recipe picks classes by tag, base and name, then changes their properties.

## Step 1: Find what to change

List every class in a level:

```bash
bf1 classes "Originals\SIDE\rep.lvl"
```

```
    9/17  wpnc  rep_weap_inf_rifle                   cannon         rep_weap_inf_rifle
    9/34  entc  rep_inf_clone_trooper                soldier        rep_inf_trooper
    9/96  ordc  rep_weap_inf_sniperrifle_ord         bolt
    9/97  wpnc  rep_weap_inf_sniperrifle             cannon         rep_weap_inf_sniperrifle
```

The columns are the class's location, tag, name and base, then its model. Look inside one class with `show`:

```bash
bf1 show "Originals\SIDE\rep.lvl" wpnc:rep_weap_inf_sniperrifle
```

```
class rep_weap_inf_sniperrifle (base cannon)
  RoundsPerClip = 6
  ReloadTime = 2.0
  ShotDelay = 1.0
  OrdnanceName = rep_weap_inf_sniperrifle_ord
  ...
```

`ShotDelay` is the time between shots in seconds, so halving it doubles the fire rate. `OrdnanceName` tells you which `ordc` class the rifle fires, and that is where its damage lives.

The Level Editor shows the same thing: open the level and click a class to see its properties.

## Step 2: Write your first rule

Create a file named `sniper-duel.json`. In Notepad, choose **Save as type: All files** so it doesn't become `sniper-duel.json.txt`. Then add:

```json
{
  "name": "Sniper Duel",
  "rules": [
    {
      "name": "sniper rifles: fire and reload faster",
      "tags": ["wpnc"],
      "classes": ["*_weap_inf_sniperrifle"],
      "set": { "ShotDelay": "*0.5", "ReloadTime": "*0.5" }
    }
  ]
}
```

- **`tags`:** only weapon classes.
- **`classes`:** only classes whose name matches the pattern. `*` stands for anything, so `*_weap_inf_sniperrifle` matches `rep_weap_inf_sniperrifle`, `cis_weap_inf_sniperrifle` and so on, every side's sniper rifle in one line.
- **`set`:** the changes. `"*0.5"` multiplies by 0.5.
- **`name`:** labels the rule in the report. It's optional but makes the report readable.

## Step 3: Apply it

```bash
bf1 apply sniper-duel.json "Originals\SIDE\rep.lvl" --out-dir "Mods\Sniper Duel"
```

```
recipe: Sniper Duel
rep.lvl: 2 values in 1 class -> Mods\Sniper Duel\rep.lvl
    sniper rifles: fire and reload faster: 2
```

The report shows how many values each rule changed in each file. The changed level goes into `--out-dir` under the same name, which is exactly what the Mod Loader expects: one folder per mod, holding the changed `.lvl` files.

Check the result with `show` on the new file:

```bash
bf1 show "Mods\Sniper Duel\rep.lvl" wpnc:rep_weap_inf_sniperrifle
```

```
  ReloadTime = 1.0
  ShotDelay = 0.5
```

Running `apply` again with the same `--out-dir` stops rather than overwrite your files. Add `--force` once you're happy to replace them.

## Step 4: Change more than one thing

Each rule is one `{ ... }` block in the `rules` list, separated by commas. Add a rule for the sniper's damage, which lives in its ordnance:

```json
    {
      "name": "sniper shots: hit harder",
      "tags": ["ordc"],
      "classes": ["*_weap_inf_sniperrifle_ord"],
      "set": { "MaxDamage": "*1.5" }
    }
```

And one for every soldier, using `base` instead of names:

```json
    {
      "name": "soldiers: tougher (heroes left alone)",
      "tags": ["entc"],
      "base": ["soldier"],
      "skip_over": 100000,
      "set": { "MaxHealth": "*1.25" }
    }
```

Heroes have huge health values like `1000000`. `skip_over` leaves any value above the number alone, so the heroes stay as they are.

## The ways to change a value

| In `set` | Does | `300.0` becomes |
|---|---|---|
| `"*1.25"` | multiply | `375.0` |
| `"*0.5"` | multiply (below 1 shrinks) | `150.0` |
| `"+4"` | add (a negative number subtracts) | `304.0` |
| `"=Veteran Trooper"` | set to this text or number | `Veteran Trooper` |

How `*` and `+` behave:
- **Vectors:** every number is changed, so `"*2"` turns `8.0 14.0` into `16.0 28.0`.
- **Number style is kept:** whole numbers stay whole (`6` plus 4 is `10`, never `10.0`), and decimals keep at least their decimal places.
- **Zero and below are skipped.** The game uses `0` and `-1` for "none" or "unlimited". The pistols' `RoundsPerClip` is `0`, because they overheat instead of reloading, so `"RoundsPerClip": "*2"` leaves them alone rather than giving them a 0-round clip.
- **Text is skipped.** A name like `rep_weap_inf_rifle` is left alone by `*` and `+`.

Property names aren't case-sensitive: `maxhealth` matches `MaxHealth`. A property that appears several times in one class, such as `FlashLightColor`, is changed everywhere it appears.

## Picking classes

A rule can use any mix of these. A class must pass all of them:

| Field | Picks | Example |
|---|---|---|
| `tags` | the class's tag | `["entc", "wpnc"]` |
| `base` | the class's base | `["hover", "flyer"]` |
| `classes` | names matching any of these patterns | `["rep_*", "cis_*"]` |
| `exclude` | then leaves out names matching these | `["*_jedi_*"]` |
| `skip_over` | leaves values above this number alone | `100000` |

With no `classes`, every class with the right tag and base is picked.

## Adding a property a class doesn't have

`=` only changes properties a class already has. To add one where it's missing, put `"add": true` on the rule. This gives the Republic rifle's bolts the thermal detonator's explosion, a property they don't have:

```json
    {
      "name": "rifle bolts explode",
      "tags": ["ordc"],
      "classes": ["rep_weap_inf_rifle_ord"],
      "set": { "ExplosionName": "=rep_weap_inf_thermaldetonator_exp" },
      "add": true
    }
```

- **Only `=` values are added.** `*` and `+` need a number to start from.
- **Name things from the same level.** The explosion has to be a class in the same `.lvl` as the ordnance, which `classes` shows. That's why this rule names the Republic's classes rather than using `*`.

## The finished recipe

```json
{
  "name": "Sniper Duel",
  "description": "Faster, harder-hitting sniper rifles for every side, tougher soldiers, cooler-running pistols and a renamed Republic trooper.",
  "rules": [
    {
      "name": "sniper rifles: fire and reload faster",
      "tags": ["wpnc"],
      "classes": ["*_weap_inf_sniperrifle"],
      "set": { "ShotDelay": "*0.5", "ReloadTime": "*0.5", "RoundsPerClip": "+4" }
    },
    {
      "name": "sniper shots: hit harder",
      "tags": ["ordc"],
      "classes": ["*_weap_inf_sniperrifle_ord"],
      "set": { "MaxDamage": "*1.5" }
    },
    {
      "name": "soldiers: tougher (heroes left alone)",
      "tags": ["entc"],
      "base": ["soldier"],
      "skip_over": 100000,
      "set": { "MaxHealth": "*1.25" }
    },
    {
      "name": "pistols: overheat slower",
      "tags": ["wpnc"],
      "classes": ["*_weap_inf_pistol"],
      "set": { "HeatPerShot": "*0.5", "ShotDelay": "*0.75" }
    },
    {
      "name": "the Republic trooper's new name",
      "tags": ["entc"],
      "classes": ["rep_inf_clone_trooper"],
      "set": { "Label": "=Veteran Trooper" }
    }
  ]
}
```

`description` is optional, like `name`; it's there for whoever reads the file.

Apply it to several levels at once by listing them all. Each changed level is written into the mod folder, and a level with nothing to change is skipped:

```bash
bf1 apply sniper-duel.json "Originals\SIDE\rep.lvl" "Originals\SIDE\cis.lvl" "Originals\SIDE\imp.lvl" "Originals\SIDE\all.lvl" --out-dir "Mods\Sniper Duel"
```

```
recipe: Sniper Duel
rep.lvl: 12 values in 8 classes -> Mods\Sniper Duel\rep.lvl
    sniper rifles: fire and reload faster: 3
    sniper shots: hit harder: 1
    soldiers: tougher (heroes left alone): 5
    pistols: overheat slower: 2
    the Republic trooper's new name: 1
cis.lvl: 20 values in 11 classes -> Mods\Sniper Duel\cis.lvl
...
```

## Step 5: Play it

Install the mod folder with the Mod Loader: pick **Sniper Duel** and press **Play**. Or from the command line:

```bash
bf1 mods install "Sniper Duel" --launch
```

## When something goes wrong

- **A rule changed 0 values:**
  - Check the class has that property with `show`; property names must be spelled exactly, though capitals don't matter.
  - Check your `classes` pattern matches with `bf1 ls FILE --name sniperrifle`.
  - A value of 0 or less, or above `skip_over`, is skipped on purpose.
- **"Can't read the recipe":** the JSON has a typo. The message gives the line and column. The usual causes are a missing comma between rules or properties, a comma after the last item in a list, or straight quotes replaced by curly ones.
- **The game crashes:**
  - Undo half your rules, then narrow down which change does it.
  - Values the game reads while a map loads, such as counts of things, are less forgiving than speeds, damage and timings.
  - Swapping a weapon's `OrdnanceName` for another weapon's ordnance crashed the game in testing when the weapon fired.
- **Nothing changed in game:** check the Mod Loader installed the files, and that you changed the side the map uses. A class can appear in several nested levels inside one file; `apply` changes every copy.

## Ideas to start from

| Goal | Rule |
|---|---|
| Faster soldiers | `entc`, base `soldier`: `"MaxSpeed": "*1.3"`, `"MaxStrafeSpeed": "*1.3"` |
| Double ammo | `entc`, base `soldier`: `"WeaponAmmo1": "*2"` through `"WeaponAmmo4": "*2"` |
| Faster shots | `ordc`: `"Velocity": "*1.5"` |
| Bigger explosions | `expc`: `"DamageRadius": "*1.5"`, `"Push": "*2"` |
| Tougher vehicles | `entc`, base `hover`, `flyer`, `walker`: `"MaxHealth": "*1.5"` |

[recipes/overdrive.json](../recipes/overdrive.json) is a bigger recipe that changes soldiers, weapons, ordnance, explosions and vehicles together. Read it for more property names, and see [CLI.md](CLI.md#recipes-many-changes-across-many-levels) for the short reference.
