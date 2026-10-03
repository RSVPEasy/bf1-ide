# bf1-ide

Modding tools for **Star Wars Battlefront (2004)**, the classic game on Steam and GOG:

- **BF1 Level Editor** opens the game's `.lvl` files so you can browse and edit what's inside. You can also import new characters and vehicles from `.glb` models.
- **BF1 Mod Loader** lets you pick a mod, swaps its `.lvl` files into the game and launches it. You can switch back to the original game at any time.

> **Help wanted:** 276 of the game's property names are still unknown. If you know SWBF1 modding, see [Help name the missing properties](#help-name-the-missing-properties).

## Download

Get `BF1 Level Editor.exe` and `BF1 Mod Loader.exe` from the [Releases](../../releases) page, or from [ModDB](https://www.moddb.com/games/star-wars-battlefront/addons/modern-bf1-lvl-editor-mod-loader). Each is a single file, so there's nothing to install. Settings are saved next to the `.exe`.

## Running from source

You need Python 3.10 or newer on Windows.

```bash
pip install -r requirements.txt
```

Start the editor with this command:

```bash
python bf1_ide.py
```

Start the mod loader with this one:

```bash
python Loader.py
```

The `swbf-unmunge.exe` file has to sit next to the scripts, because model export uses it.

## Level Editor

- A tree of every chunk in a `.lvl`, including levels nested inside other levels, with a hex view and structured views.
- Undo and redo for everything.
- Edit class properties as ODF text: health, weapons, models and the rest.
- Preview textures and import or export them as PNG or DDS. Imported textures are written the way the game's own are: DXT1 or DXT3 plus an uncompressed fallback, with a full mip chain.
- Decompile scripts to readable Lua.
- Extract the game's movies and sounds. **File → Open Movies / Sounds (.mvs, .bnk)...** lists every movie in a `.mvs` file (Bink videos, saved as `.bik`, which play in VLC) or every sample in a `.bnk` sound bank (saved as `.wav`). You can play sounds right in the list. The game stores most of these names only as hashes, so those files are named by their hash.
- Find across many files, and find everything that references a chunk.
- Export models to glTF.

### Importing a character or vehicle

Right-click a soldier's or vehicle's `modl` chunk (for example `rep_inf_trooper` or `rep_hover_fightertank`) and choose **Import Character / Vehicle (.glb)...**. Then:

1. **Texture.** Every texture in the `.glb` is packed into one texture sheet, and the model is pointed at it.
2. **Orientation.** Use the 3D view and the red, green and blue sliders to turn the model so its nose points along the yellow arrow.
3. **Size and fit.** The model is scaled into the original model's space automatically. For characters, the arm angle matches the model's pose to the game's skeleton.
4. **Textures list.** Tick **Hair** so hair follows the head instead of the arms. Tick **Delete** to leave out parts you don't want, like an extra gun or a stand.
5. **Fire points...** Drag the points shots come from onto the new model, or snap the red, floating ones onto it.
6. **Preview**, then **Import**. The low-detail (LOD) model, collision and shadow are rebuilt at the same time. The whole import is one undo step.

Each vertex is bound to the nearest bone of the original skeleton, so the game's own animations drive the new model.

Right-click **Move Fire Points...** also works on a model that's already imported.

### Engine limits worked out so far

These came from testing in the game:

- **Large parts need splitting.** The game glitches when a model segment has more than about 7,500 vertices or 30,000 indices. The importer splits big models into extra segments automatically.
- **The model header decides the detail level.** Its triangle count controls how the game treats the unit. Above about 10,000, the unit is drawn in far mode: the weapon is hidden and animation is reduced. The importer always writes 9,000 or less, however big the real model is.
- **Class-select vs gameplay.** The class-select screen uses the full model, but normal gameplay mostly shows the LOD model, so replace both.

## Mod Loader

1. **Mods folder.** Make one sub-folder per mod, holding only that mod's changed `.lvl` files, for example `My Mod\rep.lvl`.
2. **Folders.** Tell the loader where the game is, where the mods folder is, and where to find a folder of untouched original files. The originals are used to put the game back when you switch mods.
3. **Launch.** Pick a mod and press **Play**.

A mod file replaces the game file with the same name under `GameData\Data`. When a name exists more than once, any folders kept in the mod decide which file it replaces, for example `BES\bes1.lvl`.

### Starting the loader from Steam

To have **Play** in Steam open the mod loader instead of starting the game straight away:

1. **Copy the loader.** Put `BF1 Mod Loader.exe` in the game's root folder, the one that holds the `GameData` folder. For example: `C:\Program Files (x86)\Steam\steamapps\common\Star Wars Battlefront (Classic 2004)`.
2. **Open the launch options.** In Steam, right-click **Star Wars Battlefront (Classic, 2004)** → **Properties...** → **General**.
3. **Set the launch option.** Under **Launch Options**, enter the full path to the loader in quotes, followed by `%command%`:

   ```
   "C:\Program Files (x86)\Steam\steamapps\common\Star Wars Battlefront (Classic 2004)\BF1 Mod Loader.exe" %command%
   ```

Now Steam's **Play** opens the loader. Pick a mod, or **Original game**, and press **Play** to start the game. The loader finds the game folder by itself when it sits in the root folder.

## Building the .exe files

```bash
pip install pyinstaller
```

```bash
python build/build_release.py
```

This strips comments and docstrings from a copy of the code, builds the two one-file `.exe`s with PyInstaller into `dist/`, and copies the licenses next to them.


## Help name the missing properties

**If you know SWBF1 modding, this is the easiest way to help.**

The game doesn't store property names as text. It stores a hash of each name, and the editor can only show the names it knows. It knows about 96% of what the stock game uses. The rest show up as raw hashes like `0xc3f25cbb`, which makes those properties hard to edit.

[docs/UNKNOWN_PROPERTIES.md](docs/UNKNOWN_PROPERTIES.md) lists the 276 hashes still unnamed, with example values and the classes that use them. For example, `0xc3f25cbb` holds values like `p_vehicle` on building classes, so it's probably some kind of collision property.

To check a guess, run this from the repo folder:

```bash
python tools/property_hash.py BuildingCollision PlantCount
```

The tool hashes each name the way the game does (FNV-1a on the lowercased name) and prints **MATCH** if it's one of the unknown hashes. Spelling matters, but capitals don't.

**Found one?** Open an issue or a pull request with the name. To make a pull request, add the name to `COMMON_PROPERTY_NAMES` in `bf1_core.py` and remove its row from the list. Names from the original SWBF1 mod tools' `.odf` files, or from SWBF2's, which share many properties, are a good place to start.

## Credits

- The editor bundles [swbf-unmunge](https://github.com/PrismaticFlower/swbf-unmunge) by Hayden Kearns under the MIT License. See `third_party/swbf-unmunge-LICENSE.txt`.
- bf1-ide itself is released under the MIT License, see `LICENSE`.
- This project isn't affiliated with Lucasfilm, Disney, Pandemic or Aspyr. The game's files aren't included; you need your own copy of the game.
