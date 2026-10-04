"""
bf1_randomizer.py - rolls the four randomizer mods from your original side files
and puts them in the Mod Loader's mods folder, ready to pick:

  Randomizer - Safe        61 gameplay properties: health, speed, damage, blast
                           radii, fire rate, reload, range, ammo, heat, lock-on...
  Randomizer - Wild        every named property, except camera, physics springs,
                           collision, and counts/type switches read at map load
  Randomizer - Chaos       every number, nothing held back, x0.2-x5 (crashes sometimes)
  Randomizer - Pure Chaos  Chaos's numbers minus map-load counts (x0.2-x5), plus every
                           ordnance takes on another of its type from its level (a sniper
                           rifle shooting rifle bolts, missiles swapped) and every shot
                           that explodes gets a random explosion from its level:
                           sniper rifles spraying pistol bolts, rockets that pop like grenades

Every number in every unit, weapon, ordnance and explosion class (entc, wpnc,
ordc, expc) is multiplied by a random factor between --min and --max (0.5x-2x
by default); levels are rebuilt, so values can grow to any size. Models,
textures and sounds are never touched.

  python bf1_randomizer.py                      roll all four with a new seed
  python bf1_randomizer.py --seed 1234          replay a roll
  python bf1_randomizer.py --play wild          roll, install the Wild one and start the game
  python bf1_randomizer.py --modes pure --play pure

The seed is printed and saved as randomizer_last.json in each mod folder.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from pathlib import Path

import bf1_core as core
import Loader

FOLDERS = {"safe": "Randomizer - Safe", "wild": "Randomizer - Wild", "chaos": "Randomizer - Chaos",
           "pure": "Randomizer - Pure Chaos"}
CLASS_TAGS = ("entc", "wpnc", "ordc", "expc")
NUMBER = re.compile(r"^-?(\d+\.?\d*|\.\d+)$")
H = core.fnv1a32

# "safe": what you feel in a match.
SAFE = {H(n) for n in (
    "MaxHealth", "MaxSpeed", "MaxStrafeSpeed", "MaxTurnSpeed", "Acceleraton", "acceleration", "Deceleration",
    "ForwardSpeed", "ReverseSpeed", "StrafeSpeed", "TurnRate", "Traction", "DropItemProbability",
    "WeaponAmmo1", "WeaponAmmo2", "WeaponAmmo3", "WeaponAmmo4",
    "ShotDelay", "ReloadTime", "RoundsPerClip", "HeatPerShot", "HeatRecoverRate", "HeatThreshold",
    "MaxRange", "MinRange", "OptimalRange", "LockTime", "LockOnRange", "LockOnAngle", "KickStrength",
    "MinSpread", "MaxSpread", "ZoomMin", "ZoomMax", "AutoAimSize",
    "Velocity", "MaxDamage", "Damage", "LifeSpan", "Gravity", "Rebound", "LaserLength", "LaserWidth",
    "GlowLength", "BlurLength", "LightRadius", "LightColor", "LaserGlowColor",
    "DamageRadius", "DamageRadiusInner", "DamageRadiusOuter", "Push", "PushRadius", "PushRadiusInner",
    "PushRadiusOuter", "Shake", "ShakeLength", "ShakeRadius", "ShakeRadiusInner", "ShakeRadiusOuter",
    "LightDuration")}
# never in "wild": camera and physics, and counts/type switches read when a map loads
WILD_DENY = {H(n) for n in (
    "EyePointOffset", "EyePointCenter", "TrackCenter", "TrackOffset", "TiltValue", "MapScale",
    "FirstPersonFOV", "ThirdPersonFOV", "CollisionScale", "CollisionRootScale", "SkeletonRootScale",
    "AddSpringBody", "BodySpringLength", "LiftSpring", "LiftDamp", "LevelSpring", "LevelDamp",
    "VelocitySpring", "VelocityDamp", "OmegaXSpring", "OmegaXDamp", "OmegaZSpring", "OmegaZDamp",
    "BodyOmegaXSpringFactor", "AimerPitchLimits", "AimerYawLimits", "AimerPitchLimts", "AimerYawLimts",
    "PitchLimits", "YawLimits", "SetAltitude", "GravityScale", "HierarchyLevel", "NumWeapons",
    "SalvoCount", "ShotPatternCount", "NumChunks", "ChunkTerrainCollisions", "MaxItems", "LegPairCount",
    "PassengerSlots", "SpawnPointCount", "SpawnPointLocation", "WeaponChannel", "WeaponChannel1",
    "WeaponChannel2", "WeaponChannel3", "WeaponChannel4", "ForceMode", "HealthType", "AISizeType",
    "PilotType", "UnitType", "VehicleType", "FlickerType", "NormalDirection", "IsPilotExposed",
    "NoCombatInterrupt", "NoDeathExplosions", "NoEnterVehicles", "CapturePosts")}
EXPLOSION_NAME = H("ExplosionName")
NOT_SWAPPABLE = {"emitterordnance", "towcable"}  # the arc caster's emitter and the snowspeeder's cable


# The counts and type switches the game reads while a map loads - the one part of
# Chaos that "near-chaos" (Pure Chaos's numbers) leaves alone.
LOAD_TIME = {H(n) for n in (
    "SalvoCount", "ShotPatternCount", "NumChunks", "ChunkTerrainCollisions", "MaxItems", "LegPairCount",
    "PassengerSlots", "SpawnPointCount", "SpawnPointLocation", "HierarchyLevel", "NumWeapons", "WeaponChannel",
    "WeaponChannel1", "WeaponChannel2", "WeaponChannel3", "WeaponChannel4", "ForceMode", "HealthType",
    "AISizeType", "PilotType", "UnitType", "VehicleType", "IsPilotExposed", "NoEnterVehicles", "CapturePosts")}

# default multiplier range per mode (--min/--max override)
RANGES = {"safe": (0.5, 2.0), "wild": (0.5, 2.0), "chaos": (0.2, 5.0), "pure": (0.2, 5.0)}


def allowed(mode: str, prop_hash: int) -> bool:
    if mode == "chaos":
        return True
    if mode == "near-chaos":
        return prop_hash not in LOAD_TIME
    if mode == "wild":
        return prop_hash in core.HASH_TO_NAME and prop_hash not in WILD_DENY
    return prop_hash in SAFE


def roll_number(token: str, rng: random.Random, low: float, high: float, chaos: bool) -> str:
    if not NUMBER.match(token):
        return token
    x, is_int = float(token), "." not in token
    if not chaos and (x <= 0 or (is_int and x <= 1)):
        return token  # 0/1 switches and none/infinite markers
    if chaos and x == 0:
        if is_int:
            return str(rng.randint(0, 1))
        x = rng.uniform(0.0, 1.0)
    new = x * math.exp(rng.uniform(math.log(low), math.log(high)))
    if is_int:
        whole = round(abs(new))
        if abs(x) >= 1:
            whole = max(1, whole)  # a count of 3 can drop to 1, never to "none"
        return str(whole if new >= 0 else -whole)
    decimals = max(1, len(token.split(".", 1)[1]))
    text = f"{new:.4f}".rstrip("0")
    text = text + "0" if text.endswith(".") else text
    return text if len(text.split(".")[1]) >= decimals else f"{new:.{decimals}f}"


def roll_value(value: str, rng, low, high, chaos) -> str:
    tokens = value.split(" ")
    rolled = [roll_number(t, rng, low, high, chaos) for t in tokens]
    if 3 <= len(tokens) <= 4 and all(t.isdigit() and int(t) <= 255 for t in tokens):  # a colour
        rolled = [str(max(0, min(255, int(float(r))))) if NUMBER.match(r) else r for r in rolled]
    return " ".join(rolled)


def randomize_container(container, rng, mode: str, low: float, high: float, stats: dict,
                        pure_numbers: str = "near-chaos", add_explosions: bool = False) -> None:
    """One level: the classes directly in it, then each nested level on its own."""
    chunks = container.body.chunks
    blasts = []
    if mode == "pure":
        # Swap what each ordnance IS, not which ordnance a weapon names: pointing a
        # weapon's OrdnanceName at another class crashed the game on the first shot
        # (sniper rifle, pistol). So every weapon keeps firing its own class, and that
        # class takes over another one's whole definition - speed, damage, model,
        # effects, explosion. Only between ordnance of the same base, in this level.
        groups = {}
        for ch in chunks:
            if ch.tag == "ordc":
                d = core.decode_ordnance_payload(ch.payload)
                if d["base"] not in NOT_SWAPPABLE:
                    groups.setdefault(d["base"], []).append((ch, d))
            elif ch.tag == "expc":
                blasts.append(core.decode_ordnance_payload(ch.payload)["type"])
        for base, members in groups.items():
            if len(members) < 2:
                continue
            ring = members[:]
            rng.shuffle(ring)
            # each one takes the next one's definition, so nobody keeps its own
            for (ch, d), (_, donor) in zip(ring, ring[1:] + ring[:1]):
                if donor["type"] != d["type"]:
                    ch.set_payload(core.encode_ordnance_payload(base, d["type"], list(donor["props"])))
                    stats["swaps"] += 1
    for ch in chunks:
        if ch.tag == "lvl_":
            randomize_container(ch.get_nested_container(), rng, mode, low, high, stats, pure_numbers, add_explosions)
            continue
        if ch.tag not in CLASS_TAGS:
            continue
        d = core.decode_ordnance_payload(ch.payload)
        props, changed = [], False
        for key, value in d["props"]:
            h = core.key_to_hash(key)
            new = value
            numbers = pure_numbers if mode == "pure" else mode  # Pure Chaos rolls numbers like Wild by default
            if allowed(numbers, h):
                new = roll_value(value, rng, low, high, numbers in ("chaos", "near-chaos"))
                stats["values"] += sum(1 for a, b in zip(value.split(" "), new.split(" ")) if a != b)
            changed |= new != value
            props.append((key, new))
        if mode == "pure" and ch.tag == "ordc" and blasts and d["base"] not in NOT_SWAPPABLE:
            # a random explosion from its level - only for ordnance that already explodes:
            # an infantry bolt given one (it has none in the stock game) crashed when fired
            hits = [i for i, (k, _) in enumerate(props) if core.key_to_hash(k) == EXPLOSION_NAME]
            if hits or add_explosions:
                blast = rng.choice(blasts)
                for i in hits:
                    props[i] = (props[i][0], blast)
                if not hits:
                    props.append(("ExplosionName", blast))
                stats["explosions"] += 1
                changed = True
        if changed:
            ch.set_payload(core.encode_ordnance_payload(d["base"], d["type"], props))


def roll(sources: list, out_dir: Path, mode: str, seed: int, low: float, high: float,
         pure_numbers: str = "near-chaos", add_explosions: bool = False) -> dict:
    stats = {"values": 0, "swaps": 0, "explosions": 0}
    for i, src in enumerate(sources):
        container = core.parse_container(src.read_bytes())
        randomize_container(container, random.Random(f"{seed}-{mode}-{src.name}"), mode, low, high, stats,
                            pure_numbers, add_explosions)
        target = out_dir / "SIDE" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(container.raw_bytes())
    report = {"mode": mode, "seed": seed, "min": low, "max": high, "files": [s.name for s in sources], **stats}
    (out_dir / Loader.RANDOMIZER_LAST).write_text(json.dumps(report, indent=2))
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, help="replay a roll (default: a new random seed)")
    p.add_argument("--min", type=float, help="smallest multiplier (default 0.5 for safe/wild, 0.2 for chaos/pure)")
    p.add_argument("--max", type=float, help="largest multiplier (default 2 for safe/wild, 5 for chaos/pure)")
    p.add_argument("--modes", nargs="+", choices=list(FOLDERS), default=list(FOLDERS), help="which to roll (default all)")
    p.add_argument("--play", choices=list(FOLDERS), help="then install this one and start the game")
    p.add_argument("--pure-numbers", choices=("near-chaos", "wild", "chaos"), default="near-chaos",
                   help="how Pure Chaos rolls its numbers (default near-chaos: everything Chaos rolls except the "
                        "counts and type switches read when a map loads)")
    p.add_argument("--add-explosions", action="store_true",
                   help="Pure Chaos also gives explosions to ordnance that has none (infantry bolts) - crashed in testing")
    args = p.parse_args(argv)

    cfg = Loader.load_config()
    mods_dir, originals = cfg.get("mods_dir"), cfg.get("originals_dir")
    if not mods_dir or not originals:
        print("error: set the Mods folder and Original files in the Mod Loader first.", file=sys.stderr)
        return 1
    side = Path(originals) / "Data" / "_LVL_PC" / "SIDE"
    sources = sorted(side.glob("*.lvl"))
    if not sources:
        print(f"error: no side files in {side}", file=sys.stderr)
        return 1
    seed = args.seed if args.seed is not None else random.randrange(1, 1_000_000)
    print(f"seed {seed}, {len(sources)} side files")
    for mode in args.modes:
        low = args.min if args.min is not None else RANGES[mode][0]
        high = args.max if args.max is not None else RANGES[mode][1]
        r = roll(sources, Path(mods_dir) / FOLDERS[mode], mode, seed, low, high, args.pure_numbers,
                 args.add_explosions)
        print(f"  {FOLDERS[mode]:26s} x{low}-x{high} {r['values']:>7,} values" + (f", {r['swaps']} ordnance swapped, {r['explosions']} explosions" if mode == "pure" else ""))
    if args.play:
        if args.play not in args.modes:
            print(f"error: --play {args.play} wasn't rolled this time.", file=sys.stderr)
            return 1
        game = cfg.get("game_dir") or str(Loader.find_game_dir() or "")
        plan = Loader.plan(game, mods_dir, originals, FOLDERS[args.play])
        Loader.apply(game, plan, originals)
        Loader.launch(game)
        print(f"installed {FOLDERS[args.play]} and started the game")
    return 0


if __name__ == "__main__":
    sys.exit(main())
