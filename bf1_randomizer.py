"""
bf1_randomizer.py - rolls the four randomizer mods from your original side files
and puts them in the Mod Loader's mods folder, ready to pick:

  Randomizer - Safe        61 gameplay properties: health, speed, damage, blast
                           radii, fire rate, reload, range, ammo, heat, lock-on...
  Randomizer - Wild        every named property, except camera, physics springs,
                           collision, and counts/type switches read at map load
  Randomizer - Chaos       every number, nothing held back (crashes sometimes)
  Randomizer - Pure Chaos  Wild's numbers (--pure-numbers chaos for Chaos's), plus every
                           weapon fires a random ordnance of the same family (shots,
                           missiles or thrown) and every shot gets a random explosion,
                           all from its own level:
                           rifles throw grenades that go off like tank shells

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
ORDNANCE_NAME = H("OrdnanceName")
EXPLOSION_NAME = H("ExplosionName")
NOT_SWAPPABLE = {"emitterordnance", "towcable"}  # the arc caster's emitter and the snowspeeder's cable
# Ordnance only swaps within its family: a pistol handed a thrown grenade
# ("sticky") crashed the game the moment it fired.
FAMILY = {"bolt": "shot", "beam": "shot", "bullet": "shot", "missile": "missile", "sticky": "thrown", "shell": "thrown"}


def allowed(mode: str, prop_hash: int) -> bool:
    if mode == "chaos":
        return True
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
                        pure_numbers: str = "wild") -> None:
    """One level: the classes directly in it, then each nested level on its own."""
    chunks = container.body.chunks
    pool, blasts = [], []
    if mode == "pure":  # what this level carries - a weapon may only fire what's loaded with it
        for ch in chunks:
            if ch.tag == "ordc":
                d = core.decode_ordnance_payload(ch.payload)
                if d["base"] not in NOT_SWAPPABLE:
                    pool.append((d["type"], FAMILY.get(d["base"], d["base"])))
            elif ch.tag == "expc":
                blasts.append(core.decode_ordnance_payload(ch.payload)["type"])
    for ch in chunks:
        if ch.tag == "lvl_":
            randomize_container(ch.get_nested_container(), rng, mode, low, high, stats, pure_numbers)
            continue
        if ch.tag not in CLASS_TAGS:
            continue
        d = core.decode_ordnance_payload(ch.payload)
        props, changed = [], False
        for key, value in d["props"]:
            h = core.key_to_hash(key)
            new = value
            family = dict(pool).get(value)
            same_family = [name for name, fam in pool if fam == family and name != value]
            numbers = pure_numbers if mode == "pure" else mode  # Pure Chaos rolls numbers like Wild by default
            if mode == "pure" and h == ORDNANCE_NAME and family and same_family:
                new = rng.choice(same_family)
                stats["swaps"] += 1
            elif allowed(numbers, h):
                new = roll_value(value, rng, low, high, numbers == "chaos")
                stats["values"] += sum(1 for a, b in zip(value.split(" "), new.split(" ")) if a != b)
            changed |= new != value
            props.append((key, new))
        if mode == "pure" and ch.tag == "ordc" and blasts and d["base"] not in NOT_SWAPPABLE:
            # every shot gets a random explosion from its level: replace it, or add one
            blast = rng.choice(blasts)
            hits = [i for i, (k, _) in enumerate(props) if core.key_to_hash(k) == EXPLOSION_NAME]
            for i in hits:
                props[i] = (props[i][0], blast)
            if not hits:
                props.append(("ExplosionName", blast))
            stats["explosions"] += 1
            changed = True
        if changed:
            ch.set_payload(core.encode_ordnance_payload(d["base"], d["type"], props))


def roll(sources: list, out_dir: Path, mode: str, seed: int, low: float, high: float,
         pure_numbers: str = "wild") -> dict:
    stats = {"values": 0, "swaps": 0, "explosions": 0}
    for i, src in enumerate(sources):
        container = core.parse_container(src.read_bytes())
        randomize_container(container, random.Random(f"{seed}-{mode}-{src.name}"), mode, low, high, stats,
                            pure_numbers)
        target = out_dir / "SIDE" / src.name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(container.raw_bytes())
    report = {"mode": mode, "seed": seed, "min": low, "max": high, "files": [s.name for s in sources], **stats}
    (out_dir / Loader.RANDOMIZER_LAST).write_text(json.dumps(report, indent=2))
    return report


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--seed", type=int, help="replay a roll (default: a new random seed)")
    p.add_argument("--min", type=float, default=0.5, help="smallest multiplier (default 0.5)")
    p.add_argument("--max", type=float, default=2.0, help="largest multiplier (default 2.0)")
    p.add_argument("--modes", nargs="+", choices=list(FOLDERS), default=list(FOLDERS), help="which to roll (default all)")
    p.add_argument("--play", choices=list(FOLDERS), help="then install this one and start the game")
    p.add_argument("--pure-numbers", choices=("wild", "chaos"), default="wild",
                   help="how Pure Chaos rolls its numbers (default wild: chaos numbers crash about half the time)")
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
    print(f"seed {seed}, multipliers x{args.min}-x{args.max}, {len(sources)} side files")
    for mode in args.modes:
        r = roll(sources, Path(mods_dir) / FOLDERS[mode], mode, seed, args.min, args.max, args.pure_numbers)
        print(f"  {FOLDERS[mode]:26s} {r['values']:>7,} values" + (f", {r['swaps']} ordnance swaps, {r['explosions']} explosions" if mode == "pure" else ""))
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
