"""Builds the "Endless" game mode as a mod folder: every Instant Action map gets
an "(ENDLESS)" twin in the map list where both teams start with 9999
reinforcements.

  python tools/make_endless_mode.py ORIGINALS_DATA_DIR MOD_DIR

ORIGINALS_DATA_DIR is the game's untouched _LVL_PC folder. It writes:
  mission.lvl          + a copy of each Instant Action mission, named <mission>_e,
                         with SetReinforcementCount(team, 9999)
  Shell/ENG/shell.lvl  missionlist: an Endless twin of every map;
                       ifs_missionselect: picks the _e mission for a twin
  core.lvl             the "%s (ENDLESS)" label, every language

How it hangs together: the map list builds a mission name from mapluafile + the
attacking side's letter (bes1 + a = bes1a). Twins carry modesuffix = "_e", so
they load bes1a_e - a mission that's a nested level of mission.lvl like the
stock ones, named the way the campaign's bes1a_h missions are.
"""
import re
import struct
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bf1_core as core  # noqa: E402

TICKETS = 9999
SUFFIX = "_e"
LABEL_KEY = "ifs.missionselect.endless"
LABEL = "%s (ENDLESS)"


def script_of(chunk):
    return core.decode_script_chunk(chunk.payload)


def replace_script(container, name, change):
    """Decompiles script `name`, applies change(source) -> source, compiles it back."""
    for _p, _i, ch, _n in core.find_in_container(container, tag_q="scr_"):
        s = script_of(ch)
        if s["name"] == name:
            src = change(core.lua_decompile(s["code"]))
            ch.set_payload(core.encode_script_chunk(name, core.lua_compile(src)))
            return
    raise SystemExit(f"No script {name}")


def endless_missions(mission_lvl: Path) -> tuple:
    container = core.parse_container(mission_lvl.read_bytes())
    made = []
    for ch in list(container.body.chunks):
        if ch.tag != "lvl_":
            continue
        inner = ch.get_nested_container()
        scr = next((c for c in inner.body.chunks if c.tag == "scr_"), None)
        if scr is None:
            continue
        name = script_of(scr)["name"]
        if name.endswith("_h") or name.endswith(SUFFIX):
            continue
        src = core.lua_decompile(script_of(scr)["code"])
        new_src, n = re.subn(r"SetReinforcementCount\((\w+), [^)]*\)", rf"SetReinforcementCount(\1, {TICKETS})", src)
        if n == 0:
            print(f"  {name}: no SetReinforcementCount - skipped")
            continue
        new_name = name + SUFFIX
        payload = core.encode_script_chunk(new_name, core.lua_compile(new_src))
        scr_bytes = core.tagged_block(b"scr_", payload)
        lvl_payload = struct.pack("<II", core.fnv1a32(new_name), len(scr_bytes)) + scr_bytes
        container.body.add_chunk(core.Chunk("lvl_", lvl_payload, bytes(-len(lvl_payload) % 4)))
        made.append(new_name)
    return container, made


MISSIONLIST_ADDITION = f'''
-- Endless mode: a twin of every map, loading <mission>{SUFFIX} (both teams start with {TICKETS} reinforcements)
gEndlessBaseCount = getn(sp_missionselect_listbox_contents)
for i = 1, gEndlessBaseCount do
  local e = sp_missionselect_listbox_contents[i]
  local twin = {{}}
  for k, v in e do
    twin[k] = v
  end
  twin.modesuffix = "{SUFFIX}"
  sp_missionselect_listbox_contents[gEndlessBaseCount + i] = twin
end
'''


ERROR_LOG = '''-- every Lua error the game hits is appended to lua_errors.txt (in GameData)
function gModLogError(msg)
  appendto("lua_errors.txt")
  write(tostring(msg), "\\n")
  writeto()
end
_ERRORMESSAGE = gModLogError
gModLogError("missionlist loaded (error log on)")
'''


def patch_missionlist(src: str) -> str:
    src = ERROR_LOG + src
    # the map name lookup: a name ending in _e is its base map's name + " (ENDLESS)"
    old = "function missionlist_GetLocalizedMapName(p1)\n"
    assert old in src, "missionlist_GetLocalizedMapName not found"
    src = src.replace(old, old + f'''  if strlower(strsub(p1, -2)) == "{SUFFIX}" then
    local base, idx = missionlist_GetLocalizedMapName(strsub(p1, 1, -3))
    return ScriptCB_usprintf("{LABEL_KEY}", base), idx
  end
''', 1)
    return src + MISSIONLIST_ADDITION


def patch_missionselect(src: str) -> str:
    old = "local v1, v2, v3, v4 = p2.mapluafile .. p1.AttackerChar, 1, p1.CurButton, getn(gPickedMapList) + 1"
    assert old in src, "ifs_missionselect_fnSetMapAndTeam not found"
    src = src.replace(old, "local v1, v2, v3, v4 = p2.mapluafile .. p1.AttackerChar .. (p2.modesuffix or \"\"), 1, "
                           "p1.CurButton, getn(gPickedMapList) + 1")
    old = "    local v3, v4 = missionlist_GetLocalizedMapName(p2.mapluafile)\n"
    assert old in src, "MissionSelectListboxL_PopulateItem not found"
    src = src.replace(old, "    local v3, v4 = missionlist_GetLocalizedMapName(p2.mapluafile .. (p2.modesuffix or \"\"))\n", 1)
    return src


def main():
    originals, out = Path(sys.argv[1]), Path(sys.argv[2])
    out.mkdir(parents=True, exist_ok=True)

    container, made = endless_missions(originals / "mission.lvl")
    (out / "mission.lvl").write_bytes(container.raw_bytes())
    print(f"mission.lvl: {len(made)} Endless missions ({', '.join(made)})")

    shell = core.parse_container((originals / "Shell" / "ENG" / "shell.lvl").read_bytes())
    replace_script(shell, "missionlist", patch_missionlist)
    replace_script(shell, "ifs_missionselect", patch_missionselect)
    replace_script(shell, "ifs_missionselect_pcmulti", patch_missionselect)
    (out / "Shell" / "ENG").mkdir(parents=True, exist_ok=True)
    (out / "Shell" / "ENG" / "shell.lvl").write_bytes(shell.raw_bytes())
    print("shell.lvl: map list twins + mission select")

    core_lvl = core.parse_container((originals / "core.lvl").read_bytes())
    for _p, _i, ch, _n in core.find_in_container(core_lvl, tag_q="Locl"):
        s = core.decode_locl(ch.payload)
        s.set(LABEL_KEY, LABEL)
        ch.set_payload(core.encode_locl(s))
    (out / "core.lvl").write_bytes(core_lvl.raw_bytes())
    print("core.lvl: the (ENDLESS) label")


if __name__ == "__main__":
    main()
