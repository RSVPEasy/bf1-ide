# Unknown property names

The game stores each property's name as a hash, not as text. The editor can only show a name it already knows, so it shows the rest as the raw hash, like `0xc3f25cbb`.

These 276 hashes appear in the stock game's `.lvl` files but have no name yet. They're sorted by how often they're used. The values and classes are there to help you guess what each one does.

To check a guess, see [Help name the missing properties](../README.md#help-name-the-missing-properties).

| Hash | Uses | Chunk | Example values | Used by (examples) |
|---|---:|---|---|---|
| `0xc3f25cbb` | 102 | entc | `p_vehicle`, `p_vehicle1`, `p_vehicle_2` | `kas1_bldg_hut_grand`, `nab2_bldg_canal_home`, `kas2_bldg_log_bunker2` |
| `0x5d8b6dab` | 50 | entc | `1.0`, `0.8`, `0.5` | `yav_prop_grass`, `yav_prop_mist`, `yav_prop_leafpatch` |
| `0xd133f7da` | 50 | entc | `40`, `50`, `55` | `yav_prop_grass`, `yav_prop_mist`, `yav_prop_leafpatch` |
| `0x13fac25e` | 39 | entc | `0.5`, `0.4`, `0.8` | `yav_prop_grass`, `yav_prop_leafpatch`, `yav_prop_leafpatch2` |
| `0x25e5c3fc` | 39 | entc | `1.0`, `0.7`, `0.6` | `yav_prop_grass`, `yav_prop_leafpatch`, `yav_prop_leafpatch2` |
| `0x857435a7` | 33 | entc | `0.2`, `0`, `0.3` | `redlight`, `whitelight`, `bluelight` |
| `0xc2c1813a` | 33 | entc | `10.0`, `0.0`, `20.0` | `redlight`, `whitelight`, `bluelight` |
| `0xd89b1a5c` | 29 | entc | `BODY`, `TURRET1` | `bes_bldg_AAA_turret`, `com_weap_gunturret`, `geo_bldg_geoturret` |
| `0x9cbb54e2` | 29 | entc | `1.0`, `0.6`, `0.5` | `redlight`, `whitelight`, `bluelight` |
| `0x5ac02207` | 24 | entc | `1`, `6`, `4` | `nab1_prop_grass_tall`, `yav_prop_bush2`, `nab_prop_leafpatch` |
| `0x14c8d3ca` | 22 | entc | `0.0 0.0 0.0`, `0.0 0.3 0.0`, `0.0 0.4 0.0` | `yav_prop_leafpatch`, `yav_prop_leafpatch2`, `yav_prop_bush2` |
| `0x3b1948f3` | 22 | entc | `3`, `0` | `yav_prop_leafpatch`, `yav_prop_leafpatch2`, `yav_prop_bush2` |
| `0xfeb5ed07` | 18 | entc | `0.6`, `0.010` | `geo_bldg_technounion_cp`, `cis_inf_droideka`, `cis_walk_spider` |
| `0x0e3bec8e` | 18 | entc | `0.5`, `0.040`, `0.55` | `geo_bldg_technounion_cp`, `cis_inf_droideka`, `cis_walk_spider` |
| `0x276faf5c` | 17 | entc | `20.0`, `2.0`, `3.0` | `yav_bldg_tower_turret`, `bes_bldg_AAA_turret`, `hoth_bldg_dish_turret` |
| `0xb64210d2` | 17 | entc | `alliance com_icon_alliance`, `empire com_icon_imperial`, `republic com_icon_republic` | `com_bldg_controlzone`, `com_bldg_major_controlzone`, `end_bldg_bunker_controlpanel` |
| `0xfd7fae90` | 16 | entc | `0`, `6` | `yav_prop_leafpatch`, `yav_prop_leafpatch2`, `nab_prop_leafpatch` |
| `0x444f8958` | 16 | entc | `1` | `cis_tread_hailfire`, `cis_inf_droideka`, `cis_walk_spider` |
| `0xaf8e60ed` | 15 | entc | `cis_vehicle` | `bes_fly_cloudcar`, `nab_hover_gianspeeder`, `cis_fly_maf` |
| `0xa976d065` | 15 | entc | `minigun_9pose`, `speederbike_9pose`, `dishcannon_9pose` | `tat_hover_skiff`, `com_weap_gunturret`, `geo_bldg_geoturret` |
| `0x9347d123` | 14 | entc | `rep_vehicle` | `bes_fly_cloudcar`, `cis_fly_maf`, `cis_fly_droidfighter` |
| `0xb5534be0` | 12 | entc | `0.75`, `0.4`, `2.8` | `bes_bldg_AAA_turret`, `com_weap_gunturret`, `geo_bldg_geoturret` |
| `0x2436d722` | 12 | entc | `p_front_left_thigh 0.36 0.0 1.06`, `p_front_left_shin 0.36 0.0 1.06`, `p_front_left 0.5 0.0 1.1` | `imp_walk_atat` |
| `0x597c1bb7` | 11 | entc | `-15 1 -15`, `-40 0 -40`, `-60 -15 -60` | `yav_prop_mist`, `tat_prop_dust`, `bes1_prop_cloud` |
| `0x38b0b50d` | 11 | entc | `15 6 15`, `30 15 30`, `60 15 60` | `yav_prop_mist`, `tat_prop_dust`, `bes1_prop_cloud` |
| `0xb10f20ca` | 11 | entc | `2 0 2`, `-1 0 -1`, `-5 0.0 -5` | `yav_prop_mist`, `tat_prop_dust`, `bes1_prop_cloud` |
| `0xa7bcb86c` | 11 | entc | `10 1 10`, `1 0 1`, `10 0.0 10` | `yav_prop_mist`, `tat_prop_dust`, `bes1_prop_cloud` |
| `0x8624d352` | 10 | entc | `20.0`, `10.0` | `rep_fly_gunship`, `hoth_bldg_hoth_turret`, `imp_walk_atat` |
| `0x0e8662f7` | 10 | entc | `20.0`, `10.0` | `rep_fly_gunship`, `hoth_bldg_hoth_turret`, `imp_walk_atat` |
| `0x5045bcac` | 10 | entc | `321651321`, `651684651`, `5232344514` | `nab_prop_leafpatch`, `nab2_prop_leafpatch_tall`, `nab2_prop_leafpatch` |
| `0x7cbb796c` | 10 | entc | `0.06`, `0.09`, `0.12` | `nab_prop_leafpatch`, `nab2_prop_leafpatch_tall`, `nab2_prop_leafpatch` |
| `0x96b6fd80` | 9 | entc | `0`, `2`, `1` | `all_walk_tauntaun`, `cis_inf_droideka`, `cis_walk_spider` |
| `0x4bbfdeec` | 9 | entc | `20.0` | `cis_inf_droideka`, `cis_walk_spider`, `ewk_inf_scout` |
| `0x827eb1c9` | 8 | entc | `dynamic`, `Dynamic` | `com_item_powerup_ammo`, `com_item_weaponrecharge`, `com_item_healthrecharge` |
| `0x8cf29543` | 8 | entc | `BldgCylinder aacylinder NULL 0.0 4.7 0.0...`, `BldgCylinder aacylinder NULL 0.0 0.75 0....`, `BldgSphere1 sphere NULL 3.0 9.0 -3.0 5.0...` | `cis_walk_spider`, `com_item_vehiclerecharge`, `imp_walk_atst` |
| `0x630c0e9e` | 8 | entc | `2.2` | `tat_bldg_tuskanspawn`, `com_bldg_controlzone`, `com_bldg_untakeable_controlzone` |
| `0xad7915d2` | 7 | entc | `TURRET1` | `bes_bldg_AAA_turret`, `com_weap_gunturret`, `geo_bldg_geoturret` |
| `0x1d56b73b` | 7 | entc | `grab hp_hotspot 5.0`, `grab1 hp_hotspot1 5.0`, `grab2 hp_hotspot2 5.0` | `tat1_bldg_sarlacctentacle`, `bes2_carbon_forklift` |
| `0xa2a034ea` | 7 | entc | `com_weap_powerup_pickup defer`, `com_weap_ammo_pickup defer`, `com_item_gonkrecharge defer` | `com_item_powerup_ammo`, `com_item_powerup_health100`, `com_item_powerup_health25` |
| `0x6d8fe606` | 7 | entc | `1` | `nab_prop_trees2`, `tat1_bldg_sarlacctentacle`, `tat1_bldg_sarlaccpit` |
| `0x6b8d8679` | 7 | entc | `1.0`, `1.5`, `2.0` | `cis_inf_droideka`, `cis_walk_spider`, `imp_walk_atat` |
| `0x5b491acc` | 7 | entc | `1.0` | `cis_inf_droideka`, `cis_walk_spider`, `imp_walk_atat` |
| `0x829299ad` | 7 | entc | `10.0` | `cis_inf_droideka`, `cis_walk_spider`, `imp_walk_atat` |
| `0x07fe74d0` | 7 | entc | `atat_explosion`, `small_explosion`, `med_explosion` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0xf2aa4a90` | 6 | entc | `0.0 8.0 0.0`, `0.0 -1.0 -15.0`, `1.5 -0.75 1.75` | `bes_fly_cloudcar`, `imp_fly_tiebomber`, `imp_walk_atst` |
| `0xf138015c` | 6 | entc | `explosion`, `super_explosion hp_explode1`, `super_explosion hp_explode2` | `geo_bldg_technounion_cp`, `end_weap_treesmash` |
| `0xd4cbd35b` | 6 | entc | `0.5`, `1.7`, `1.8` | `nab_prop_leafpatch`, `nab2_prop_leafpatch_tall`, `nab2_prop_leafpatch` |
| `0x231fcc79` | 6 | entc | `0.25 0.4`, `0.2 0.4`, `0.2 0.3` | `nab1_prop_grass_tall`, `nab1_grass`, `nab1_grass_close` |
| `0xa9b14958` | 6 | entc | `1.6`, `3.0`, `1.7` | `nab1_prop_grass_tall`, `nab1_grass`, `nab1_grass_close` |
| `0xf6582b9c` | 6 | entc | `0.15`, `0.2`, `0.22` | `nab1_prop_grass_tall`, `nab1_grass`, `nab1_grass_close` |
| `0xff7bb39a` | 6 | entc | `1.0`, `2.3`, `0.6` | `nab1_prop_grass_tall`, `nab1_grass`, `nab1_grass_close` |
| `0x4b6d5171` | 6 | entc | `4`, `20`, `2` | `nab1_prop_grass_tall`, `nab1_grass`, `nab1_grass_close` |
| `0x07ff5a07` | 6 | entc | `walkerstomp`, `bigwalkerstomp` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0x391bbcda` | 6 | entc | `5`, `10.0`, `16` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0x97157af6` | 6 | entc | `com_weap_layered_at_step`, `cis_walk_spider_step_layered` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0x0ffe3c06` | 5 | entc | `1` | `tat_hover_skiff`, `bes_fly_cloudcar`, `imp_hover_speederbike` |
| `0xfec8cea9` | 5 | entc | `VhclCylinder aacylinder NULL 0.0 6.0 0.0...`, `VehcCylinder aacylinder NULL 0.0 0.75 0....`, `VehBlockEntry2 aacylinder NULL 0.0 0.0 0...` | `tat1_bldg_sarlaccpit`, `com_item_vehiclerecharge`, `end_prop_ewok_fire` |
| `0x02565bd1` | 5 | entc | `build`, `aligned` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x09f126c2` | 5 | entc | `35.0`, `200.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xb761134e` | 5 | entc | `0.75`, `3.5`, `2.5` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x3d60bcc8` | 5 | entc | `1.0`, `2.5`, `1.5` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xf31c745f` | 5 | entc | `0.3`, `1.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xf0ac6de0` | 5 | entc | `0.0`, `1.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x543722af` | 5 | entc | `0.5` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x4cecac28` | 5 | entc | `0.0`, `1.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x1d7b0711` | 5 | entc | `0.0`, `8.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xaff1f98c` | 5 | entc | `0.2` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x186a4586` | 5 | entc | `0.01` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xa0f13d29` | 5 | entc | `0.01` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x78fd34b7` | 5 | entc | `0.4` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x8710d279` | 5 | entc | `0.8` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0x9fe971e5` | 5 | entc | `2.0` | `com_holo_turret`, `com_holo_controlzone`, `com_holo_wall` |
| `0xc3b6f18f` | 5 | entc | `0.0` | `geo_bldg_technounion_cp` |
| `0xba8411d3` | 5 | entc | `vehicleflame`, `vehiclespark`, `vehiclesmoke` | `geo_bldg_technounion_cp` |
| `0xb69cfbab` | 5 | entc | `1.5` | `geo_bldg_technounion_cp` |
| `0x27d19892` | 5 | entc | `0.0` | `geo_bldg_technounion_cp` |
| `0x5fcc23ff` | 5 | entc | `hp_damage2`, `hp_damage1`, `hp_damage3` | `geo_bldg_technounion_cp` |
| `0xadf73cb1` | 5 | entc | `0` | `cis_fly_geofighter`, `cis_tread_hailfire`, `imp_walk_atat` |
| `0x069134e5` | 5 | entc | `atst_leg_up`, `cis_walk_spider_legup` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0x8ae91462` | 5 | entc | `0.2`, `.5`, `0.5` | `cis_walk_spider`, `imp_walk_atat`, `imp_walk_atst` |
| `0x871eb169` | 5 | entc | `1.5` | `imp_walk_atat`, `imp_walk_atst`, `imp_walk_atst_jungle` |
| `0x58e1ba4e` | 5 | entc | `imp_walk_atst_finalexp`, `imp_walk_atat_finalexp`, `rep_walk_atte_exp` | `imp_walk_atat`, `imp_walk_atst`, `imp_walk_atst_jungle` |
| `0xa47728f9` | 5 | entc | `5.5808 1.9066 -0.8685`, `1.97 0.0 11.43`, `0.0 0.0 0.0` | `imp_walk_atat`, `imp_walk_atst`, `imp_walk_atst_jungle` |
| `0xe04f08c1` | 4 | entc | `bes_fly_cloudcar_exp`, `bes_bldg_AAA_turret_exp`, `cis_hover_aat_exp` | `bes_fly_cloudcar`, `bes_bldg_AAA_turret`, `cis_hover_aat` |
| `0x48e46a1b` | 4 | entc | `20.0`, `3.0` | `yav_bldg_tower_turret`, `bes_bldg_AAA_turret`, `hoth_bldg_dish_turret` |
| `0xf33b41df` | 4 | entc | `75`, `50`, `100` | `com_item_powerup_health100`, `com_item_powerup_health25`, `com_item_powerup_dual` |
| `0x6a83ed9c` | 4 | entc | `com_holo_controlzone`, `com_holo_controlzone_small` | `com_bldg_controlzone`, `com_bldg_major_controlzone`, `end_bldg_bunker_controlpanel` |
| `0xa33d3401` | 4 | entc | `0.4` | `tat_bldg_tuskanspawn`, `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0x6e0ba2d4` | 4 | entc | `1.0` | `greenlight` |
| `0xbd94b44e` | 4 | entc | `6000000`, `30.0`, `50.0` | `geo_bldg_geoturret`, `nab_bldg_fambaa_generator`, `nab_bldg_fambaa_shield` |
| `0x0e615de7` | 4 | entc | `70.0`, `55.0`, `40.0` | `geo_bldg_technounion_cp` |
| `0x0df468ca` | 4 | entc | `nab1_prop_gunganhead_lowrez` | `nab1_prop_gunganhead`, `nab1_prop_gunganhead_bunker` |
| `0x3d5d0aef` | 4 | entc | `0.0` | `nab1_grass`, `nab1_grass_close` |
| `0x97133eeb` | 4 | entc | `0.01`, `0.5` | `nab1_grass`, `nab1_grass_close` |
| `0x9c3f7a19` | 4 | entc | `1` | `nab1_grass`, `nab1_grass_close` |
| `0xcdbc383d` | 4 | entc | `2.0 0.0`, `0.0 0.9` | `nab2_prop_vine1`, `nab2_prop_vine2` |
| `0xa340d6b5` | 4 | entc | `10` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x00e73245` | 4 | entc | `10` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x4db44670` | 4 | entc | `10` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x9e78b410` | 4 | entc | `10` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x573c98a4` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x44a2c4a6` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x5bf85ae8` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0xa8679457` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x8f5540cb` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0xe689ef93` | 4 | entc | `0` | `all_hover_hovernaut`, `cis_hover_mtt`, `imp_walk_atat` |
| `0x02bb1fe0` | 4 | entc, ordc | `cis_weap_inf_wrist_rocket_exp`, `cis_hover_aat_exp`, `cis_hover_mtt_exp` | `cis_weap_inf_wrist_rocket_ord`, `cis_hover_aat`, `cis_hover_mtt` |
| `0x26239cd4` | 4 | entc | `0.5`, `0.6` | `tat_inf_jawa`, `ewk_inf_scout`, `ewk_inf_repair` |
| `0xc4b2cb20` | 4 | entc | `21.0`, `8.0`, `5.0` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0xf0b75e90` | 4 | entc | `8.0`, `0.0` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0x69c4d0dd` | 4 | entc | `darktrooper_jetpack`, `geo_wingsfly`, `jetpack` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0x0825c1bf` | 4 | entc | `0.06`, `0.03` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0x36d9de4d` | 4 | entc | `0.0`, `0.1`, `0.2` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0x1ebb5841` | 4 | entc | `0.45`, `0.2`, `0.25` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0xf8a37c8a` | 4 | entc | `0.45`, `0.3`, `0.24` | `geo_inf_geonosian`, `imp_inf_dark_trooper`, `imp_inf_dark_troopersnow` |
| `0x934d56e0` | 4 | entc | `2.0`, `1.0` | `imp_walk_atat`, `imp_walk_atst`, `imp_walk_atst_jungle` |
| `0x08aab3aa` | 4 | entc | `1.0` | `imp_walk_atat`, `imp_walk_atst`, `imp_walk_atst_jungle` |
| `0xc3da529f` | 3 | entc | `1`, `2`, `1.0` | `com_item_powerup_ammo`, `com_item_powerup_dual`, `com_item_weaponrecharge` |
| `0x299acaba` | 3 | entc | `1.0`, `2.0` | `com_item_weaponrecharge`, `com_item_healthrecharge`, `com_item_vehiclerecharge` |
| `0x044894ab` | 3 | entc | `1.75`, `3.0` | `com_item_weaponrecharge`, `com_item_healthrecharge`, `com_item_vehiclerecharge` |
| `0x48542041` | 3 | entc | `com_holo_turret`, `com_holo_wall`, `com_holo_trap` | `com_weap_gunturret`, `com_prop_barrier_02`, `end_weap_treesmash` |
| `0x97c931c5` | 3 | entc | `com_prop_buildzone`, `end_weap_treesmash` | `com_weap_gunturret`, `com_prop_barrier_02`, `end_weap_treesmash` |
| `0x862be6b4` | 3 | entc | `1`, `0.0`, `1.1` | `end_prop_tree01`, `end_prop_tree04`, `end_prop_tree02` |
| `0x5fac58b5` | 3 | entc | `fambaa_death`, `destroy` | `geo_bldg_technounion_cp`, `nab_bldg_fambaa_generator`, `nab_bldg_fambaa_shield` |
| `0x22624b58` | 3 | wpnc | `1` | `imp_weap_fly_tiefighter_cannon`, `cis_weap_fly_droidfighter_cannon` |
| `0x7c8816fc` | 3 | entc | `deathdustcloud_brown`, `ginormousdustcloud` | `cis_inf_droideka`, `cis_walk_spider`, `rep_walk_atte` |
| `0xae640110` | 3 | entc | `5.1` | `cis_inf_droideka`, `cis_walk_spider`, `rep_walk_atte` |
| `0x060c07e2` | 3 | entc | `0.0 0.0 0.0` | `cis_inf_droideka`, `cis_walk_spider`, `rep_walk_atte` |
| `0xb32208ed` | 3 | entc | `1` | `cis_walk_spider`, `imp_walk_atat`, `rep_walk_atte` |
| `0x6be6199f` | 3 | entc | `com_inf_wading` | `gun_inf_soldier`, `gun_inf_defender`, `wok_inf_warrior` |
| `0x0ebb87c8` | 3 | entc | `1.0`, `0.5` | `imp_walk_atat`, `rep_fly_gunship` |
| `0x47a95951` | 3 | entc | `30.0` | `imp_walk_atst`, `imp_walk_atst_jungle`, `imp_walk_atst_snow` |
| `0x7335aa87` | 3 | entc | `0.7` | `imp_walk_atst`, `imp_walk_atst_jungle`, `imp_walk_atst_snow` |
| `0x2c70cf18` | 2 | entc | `pH_tester_arm10`, `pH_tester_arm6` | `com_item_healthrecharge` |
| `0xb135436f` | 2 | entc | `255 255 255` | `com_holo_controlzone`, `com_holo_controlzone_small` |
| `0x68daede1` | 2 | entc | `30 220 30` | `com_holo_controlzone`, `com_holo_controlzone_small` |
| `0x82e6120a` | 2 | entc | `240 30 30` | `com_holo_controlzone`, `com_holo_controlzone_small` |
| `0x9eb4d884` | 2 | entc | `240 220 30` | `com_holo_controlzone`, `com_holo_controlzone_small` |
| `0xf1a8f31b` | 2 | entc | `12.0`, `36.0` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0xeb1bcbf8` | 2 | entc | `10.0`, `16.0` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0xf17a02e6` | 2 | entc | `com_blg_commandpost_capture defer` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0x16e4a90e` | 2 | entc | `com_blg_commandpost_discharge defer` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0x8e39006a` | 2 | entc | `com_blg_commandpost_lost defer` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0x12caacaa` | 2 | entc | `com_blg_commandpost_dispute defer` | `com_bldg_controlzone`, `com_bldg_major_controlzone` |
| `0xc461d811` | 2 | entc | `1.0`, `3.0` | `com_weap_inf_landmine`, `com_weap_inf_empmine` |
| `0xf357466a` | 2 | entc | `8` | `end_prop_godraycluster`, `yav_prop_godraycluster` |
| `0x604db82e` | 2 | entc | `1` | `nab1_prop_grass_tall` |
| `0xda3491bc` | 2 | wpnc | `100000`, `4000` | `nab_weap_bldg_fambaa_shield`, `cis_weap_walk_droideka_shield` |
| `0xbad94c15` | 2 | wpnc | `-100000`, `-100` | `nab_weap_bldg_fambaa_shield`, `cis_weap_walk_droideka_shield` |
| `0x5a9201b6` | 2 | wpnc | `60.0 30.0`, `1.25` | `nab_weap_bldg_fambaa_shield`, `cis_weap_walk_droideka_shield` |
| `0xddb924a1` | 2 | wpnc | `fambaa_shield`, `droidekashield` | `nab_weap_bldg_fambaa_shield`, `cis_weap_walk_droideka_shield` |
| `0xb371fc9a` | 2 | entc | `0` | `nab_bldg_fambaa_generator`, `nab_bldg_fambaa_shield` |
| `0x9d39f68a` | 2 | entc | `1` | `nab_bldg_fambaa_generator`, `nab_bldg_fambaa_shield` |
| `0x35a4393b` | 2 | entc | `2.8 2.8` | `nab_prop_flowers` |
| `0xe8888b42` | 2 | entc | `CAPE` | `cis_inf_countdooku`, `imp_inf_darthvader` |
| `0x94272331` | 2 | entc | `cis_inf_countdooku_cape`, `imp_inf_vader_cape` | `cis_inf_countdooku`, `imp_inf_darthvader` |
| `0xdddcde45` | 2 | entc | `cis_inf_dooku_cape`, `imp_inf_vader_cape` | `cis_inf_countdooku`, `imp_inf_darthvader` |
| `0xa423e008` | 2 | entc | `bone_ribcage` | `cis_inf_countdooku`, `imp_inf_darthvader` |
| `0x65587105` | 2 | entc | `1` | `cis_walk_spider`, `rep_walk_atte` |
| `0x0b9bb9c9` | 2 | entc | `22.0`, `10.0` | `cis_walk_spider`, `rep_walk_atte` |
| `0x96157963` | 2 | entc | `cis_walk_spider_step_layered`, `com_weap_layered_at_step` | `cis_walk_spider`, `imp_walk_atat` |
| `0x91fabb5c` | 2 | entc | `hover` | `geo_inf_geonosian`, `rep_inf_jet_trooper` |
| `0xb9282909` | 2 | entc | `0.5` | `imp_hover_speederbike`, `rep_hover_speederbike` |
| `0x04afc6a4` | 2 | entc | `0.8` | `imp_hover_speederbike`, `rep_hover_speederbike` |
| `0x0df83e75` | 2 | entc | `hp_fire_Lcannon`, `hp_fire_Rcannon` | `imp_walk_atat` |
| `0xf07b5b62` | 2 | entc | `-0.9 0.9` | `rep_walk_atte` |
| `0xf949d898` | 2 | entc | `sarlacc_belch` | `tat1_bldg_sarlacctentacle` |
| `0x0550a3b2` | 2 | entc | `1` | `tat1_bldg_sarlacctentacle` |
| `0x6b014234` | 1 | entc | `1` | `bes2_carbon_forklift` |
| `0x1b5b62ea` | 1 | entc | `gonk1` | `com_item_weaponrecharge` |
| `0x19e0afd9` | 1 | entc | `0.08` | `com_item_weaponrecharge` |
| `0x1c1b9c83` | 1 | entc | `foot_l2` | `com_item_weaponrecharge` |
| `0x6eefb998` | 1 | entc | `foot_r1` | `com_item_weaponrecharge` |
| `0x18275a73` | 1 | entc | `Equipment_Operator_Arm_Socket` | `com_item_healthrecharge` |
| `0xe728d955` | 1 | entc | `1` | `com_item_vehiclerecharge` |
| `0x7cdccf2a` | 1 | entc | `TerrCylinder aacylinder NULL 0.0 0.75 0....` | `com_item_vehiclerecharge` |
| `0x07d00083` | 1 | entc | `SoldCylinder aacylinder NULL 0.0 0.75 0....` | `com_item_vehiclerecharge` |
| `0x330acaab` | 1 | entc | `OrdnCylinder aacylinder NULL 0.0 0.75 0....` | `com_item_vehiclerecharge` |
| `0x19755b4b` | 1 | entc | `HUD_all_lascannon_icon` | `com_weap_gunturret` |
| `0xce754894` | 1 | entc | `2.5` | `com_weap_inf_empmine` |
| `0x06b4bbb5` | 1 | entc | `1` | `end_weap_treesmash` |
| `0xf7a5d75b` | 1 | entc | `trigger` | `end_weap_treesmash` |
| `0x2fd8a07b` | 1 | entc | `reset` | `end_weap_treesmash` |
| `0x619005ab` | 1 | entc | `hp_active` | `end_weap_treesmash` |
| `0x138b5187` | 1 | entc | `com_prop_buildzone` | `end_weap_treesmash` |
| `0xabcb950a` | 1 | entc | `imp` | `end_weap_treesmash` |
| `0x2e4c2b79` | 1 | entc | `all` | `end_weap_treesmash` |
| `0x479e5b63` | 1 | entc | `4.0` | `end_weap_treesmash` |
| `0xb4c6360c` | 1 | entc | `0.666` | `end_weap_treesmash` |
| `0x29824727` | 1 | entc | `0.0 7.5 0.0` | `end_weap_treesmash` |
| `0xaf922a6b` | 1 | ordc | `5.0` | `geo_weap_bldg_geoturret_ord` |
| `0xaee05d99` | 1 | ordc | `0.0` | `geo_weap_bldg_geoturret_ord` |
| `0x3ed11ac3` | 1 | ordc | `0.0` | `geo_weap_bldg_geoturret_ord` |
| `0x58a7da24` | 1 | ordc | `1.0` | `geo_weap_bldg_geoturret_ord` |
| `0xa4c53efd` | 1 | ordc | `3.0` | `geo_weap_bldg_geoturret_ord` |
| `0x0403d917` | 1 | entc | `geo_bldg_technounion_hulk` | `geo_bldg_technounion_cp` |
| `0x3e4e9ba0` | 1 | entc | `1` | `geo_bldg_technounion_cp` |
| `0xf31d1478` | 1 | entc | `1` | `geo_bldg_technounion_hull` |
| `0x4456e84a` | 1 | entc | `0.1` | `hot_rum_caves` |
| `0x25779a5c` | 1 | entc | `1.0` | `hot_rum_caves` |
| `0xa4fbdbc8` | 1 | entc | `.5` | `hot_rum_caves` |
| `0xf440c23a` | 1 | entc | `.5` | `hot_rum_caves` |
| `0x1c5461b1` | 1 | entc | `1.0` | `hot_rum_caves` |
| `0x78221603` | 1 | entc | `1.0` | `hot_rum_caves` |
| `0x1cb9e68d` | 1 | entc | `0.0` | `hot_rum_caves` |
| `0x7189dcc3` | 1 | entc | `0.0` | `hot_rum_caves` |
| `0xbf6413ae` | 1 | entc | `0.1` | `hot_rum_caves` |
| `0x220abe04` | 1 | entc | `0.75` | `hot_rum_caves` |
| `0x4104158d` | 1 | entc | `0.25` | `hot_rum_caves` |
| `0x931a6a3b` | 1 | entc | `1.0` | `hot_rum_caves` |
| `0xf8c69a26` | 1 | entc | `2.0` | `hot_rum_caves` |
| `0x8b37cab4` | 1 | entc | `15.0` | `hot_rum_caves` |
| `0x4fb4269f` | 1 | entc | `0.2` | `hot_rum_caves` |
| `0xc3e9c081` | 1 | entc | `1.0` | `hot_rum_caves` |
| `0xcff18812` | 1 | entc | `0.1` | `hot_rum_caves` |
| `0x626eeba0` | 1 | entc | `0.75` | `hot_rum_caves` |
| `0x75096fab` | 1 | entc | `0.0` | `hothlight3` |
| `0xa0fff23e` | 1 | entc | `0.0` | `hothlight3` |
| `0xa25f865f` | 1 | wpnc | `1` | `nab_weap_bldg_fambaa_generator` |
| `0xb6ebf796` | 1 | entc | `0 0 -1` | `nab_bldg_fambaa_generator` |
| `0x729dfa7a` | 1 | wpnc | `300` | `cis_weap_walk_droideka_shield` |
| `0x1d726313` | 1 | wpnc | `0.0 1.0 0.125` | `cis_weap_walk_droideka_shield` |
| `0xf1f9f3e3` | 1 | entc | `cis_droideka_idle_low1` | `cis_inf_droideka` |
| `0x19d9ab85` | 1 | entc | `cis_droideka_roll_low1` | `cis_inf_droideka` |
| `0x9895bb7a` | 1 | entc | `0.65` | `cis_inf_droideka` |
| `0xacacdb0c` | 1 | entc | `10.0` | `cis_inf_droideka` |
| `0xed895ee0` | 1 | entc | `0.1` | `cis_inf_droideka` |
| `0x578f6ae6` | 1 | entc | `2.0` | `cis_inf_droideka` |
| `0xdc524dd9` | 1 | entc | `2.0` | `cis_inf_droideka` |
| `0x09a7a164` | 1 | entc | `0.2` | `cis_inf_droideka` |
| `0xde6b8538` | 1 | entc | `100.0` | `cis_inf_droideka` |
| `0x6560de67` | 1 | entc | `50.0` | `cis_inf_droideka` |
| `0x8f720e3c` | 1 | entc | `p_rollsphere` | `cis_inf_droideka` |
| `0xd2da8f5e` | 1 | entc | `10.0` | `cis_inf_droideka` |
| `0xe6f7befb` | 1 | entc | `35.0` | `cis_inf_droideka` |
| `0xf01d5583` | 1 | entc | `1.5` | `cis_inf_droideka` |
| `0xac229931` | 1 | entc | `0.5` | `cis_inf_droideka` |
| `0x21f56ed0` | 1 | entc | `cis_inf_droideka_step` | `cis_inf_droideka` |
| `0x937ceaa5` | 1 | entc | `cis_inf_droideka_step` | `cis_inf_droideka` |
| `0x12b23362` | 1 | entc | `cis_inf_droideka_step` | `cis_inf_droideka` |
| `0x7a5de0fc` | 1 | entc | `1` | `cis_tread_hailfire` |
| `0xe534c2cb` | 1 | entc | `CIS_HailFire_Droid_center` | `cis_tread_hailfire` |
| `0xabb24116` | 1 | entc | `armL` | `cis_tread_hailfire` |
| `0x580b459f` | 1 | entc | `armR` | `cis_tread_hailfire` |
| `0xf0856c5d` | 1 | entc | `-0.5` | `cis_tread_hailfire` |
| `0x8eda7a09` | 1 | entc | `-0.125` | `cis_tread_hailfire` |
| `0xdb873344` | 1 | ordc | `0` | `ewk_weap_inf_spear_ord` |
| `0x2bfc41ee` | 1 | entc | `1` | `gar_inf_soldier` |
| `0x10e3903c` | 1 | entc | `5.0` | `geo_inf_geonosian` |
| `0x2afc405b` | 1 | entc | `1` | `gun_inf_defender` |
| `0x215ce4d0` | 1 | entc | `small_explosion` | `imp_walk_atat` |
| `0x3a714b27` | 1 | entc | `imp_walk_atat1` | `imp_walk_atat` |
| `0x0c13fc70` | 1 | entc | `3.5` | `imp_walk_atat` |
| `0x94b9538c` | 1 | entc | `1.0` | `imp_walk_atat` |
| `0xbb80d985` | 1 | entc | `1.0` | `imp_walk_atat` |
| `0xb3af758b` | 1 | entc | `30.0` | `imp_walk_atat` |
| `0x7fdeedf1` | 1 | entc | `1` | `imp_walk_atat` |
| `0x9cc82b1f` | 1 | ordc | `2.0` | `rep_weap_inf_arccaster_ord` |
| `0xa00a48cd` | 1 | ordc | `4` | `rep_weap_inf_arccaster_ord` |
| `0xfc0d5e74` | 1 | ordc | `75` | `rep_weap_inf_arccaster_ord` |
| `0xcca74473` | 1 | ordc | `75` | `rep_weap_inf_arccaster_ord` |
| `0xad37744e` | 1 | ordc | `20` | `rep_weap_inf_arccaster_ord` |
| `0x6dd5a894` | 1 | ordc | `arccaster_lightning` | `rep_weap_inf_arccaster_ord` |
| `0x2e5375da` | 1 | entc | `hp_turret_left` | `rep_fly_gunship` |
| `0x95437337` | 1 | entc | `gen_wings` | `rep_inf_macewindu` |
| `0x7136fd3e` | 1 | entc | `65536` | `rep_walk_atte` |
| `0x3e2c4da4` | 1 | entc | `hp_link_1` | `rep_walk_atte` |
| `0x9b158142` | 1 | entc | `com_weap_layered_at_step` | `rep_walk_atte` |
| `0x9c1582d5` | 1 | entc | `com_weap_layered_at_step` | `rep_walk_atte` |
| `0xf3e10235` | 1 | entc | `soldierstepleft` | `wok_inf_warrior` |
| `0x58a1f462` | 1 | entc | `soldierstepright` | `wok_inf_warrior` |
| `0xdd807455` | 1 | entc | `yav_bldg_watchtower_hulk` | `yav_bldg_tower_turret` |
| `0x8b6e7dc3` | 1 | entc | `STATIC` | `yav_bldg_tower_turret` |
| `0x88e0a44b` | 1 | entc | `mediumsmoketrail` | `yav_bldg_tower_turret` |
| `0xc74113de` | 1 | entc | `smokeplume` | `yav_bldg_tower_turret` |
| `0x7aded410` | 1 | entc | `hp_smoke1` | `yav_bldg_tower_turret` |
