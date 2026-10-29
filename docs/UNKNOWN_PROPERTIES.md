# Unknown property names

The game stores each property's name as a hash, not as text. The editor can only show a name it already knows, so it shows the rest as the raw hash, like `0x19755b4b`.

These 8 hashes appear in the stock game's `.lvl` files but have no name yet. They're sorted by how often they're used. The values and classes are there to help you guess what each one does.

To check a guess, see [Help name the missing properties](../README.md#help-name-the-missing-properties).

| Hash | Uses | Chunk | Example values | Used by (examples) |
|---|---:|---|---|---|
| `0x19755b4b` | 1 | entc | `HUD_all_lascannon_icon` | `com_weap_gunturret` |
| `0x0403d917` | 1 | entc | `geo_bldg_technounion_hulk` | `geo_bldg_technounion_cp` |
| `0x215ce4d0` | 1 | entc | `small_explosion` | `imp_walk_atat` |
| `0xdd807455` | 1 | entc | `yav_bldg_watchtower_hulk` | `yav_bldg_tower_turret` |
| `0x8b6e7dc3` | 1 | entc | `STATIC` | `yav_bldg_tower_turret` |
| `0x88e0a44b` | 1 | entc | `mediumsmoketrail` | `yav_bldg_tower_turret` |
| `0xc74113de` | 1 | entc | `smokeplume` | `yav_bldg_tower_turret` |
| `0x7aded410` | 1 | entc | `hp_smoke1` | `yav_bldg_tower_turret` |
