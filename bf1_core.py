"""
bf1_core.py - Core binary format layer for the BF1 IDE.

Merges and bug-fixes the parsing/encoding logic that used to live in two
separate scripts (bf1_lvl_unmunger.py and bf1_ord_parser.py).

WHAT CHANGED VS. THE ORIGINAL TWO SCRIPTS
------------------------------------------
1. Property-hash round trip no longer corrupts unknown properties.
   The old ord parser printed unrecognized hashes as "0xdeadbeef" text and
   then, if you converted that text back to binary, hashed the *string*
   "0xdeadbeef" instead of recovering the original integer. Any property
   not in the name table was silently corrupted on a round trip. Fixed by
   `key_to_hash()` / `hash_to_key()` below, which special-case the
   "0x########" pattern.

2. Three wrong entries in the property name table.
   TurnRate, PersonScale and BuildingScale were hand-transcribed wrong
   (verified by recomputing FNV-1a for every entry). Corrected here.

3. Chunk type (ordc vs wpnc vs expc) is no longer guessed from a filename
   extension. The old CLI guessed the output tag from the *input* file's
   suffix, which after an .odf round trip was always ".odf" -> always
   guessed "ordc", silently turning weapons/explosives into ordnance.
   Here, every parsed chunk keeps its real tag as part of the live object
   graph, so it's never re-guessed.

4. Lossless structural parsing. The old unmunger used a small hardcoded
   allow-list of ~20 known 4-byte tags to decide where a chunk stream
   "really" starts, silently discarding any leading bytes it skipped over
   (never recorded anywhere) and outright dropping any trailing bytes that
   didn't form a complete chunk. `parse_body()` below captures *any*
   leading prefix and *any* unparsed trailing bytes explicitly, so nothing
   is thrown away, and it no longer needs a hardcoded tag allow-list to
   decide chunk boundaries (it trusts the length field the same way the
   original did, but never discards what it skips).

5. Nested "lvl_" chunks are no longer silently re-serialized on every
   rebuild. The old code's docstring promised nested-chunk browsing
   couldn't corrupt anything, but `rebuild_lvl` actually *did* rebuild
   every "lvl_" chunk from its extracted ".embedded" folder if present,
   whether or not you'd touched it - and that folder's own extraction
   used the same lossy heuristic from #4. Here, a chunk's raw bytes are
   only ever replaced if something inside it was actually edited (see
   `Chunk.set_payload` / `Body.notify_changed` dirty propagation). Nested
   chunks you never open are guaranteed byte-identical on save.

6. "Add a brand new chunk" is wired up. The old `_pack_chunk` /
   `_pack_child_payload` helpers existed but were never called from the
   rebuild path. `pack_new_chunk()` here is actually used by the IDE.
"""
from __future__ import annotations

import re
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field
import math
import io
import wave
import json
from dataclasses import dataclass
import heapq

MAGIC = b"ucfb"

# --- FNV-1a 32-bit, matching the Zero Engine's string hashing ------------


def fnv1a32(value: str) -> int:
    """The engine ORs every byte with 0x20 rather than lowercasing - the same
    for letters, but it changes '_' (and @[\\]^), so names with underscores
    (e.g. every sound sample: wpn_cis_sniperRifle_fire) only match this way.
    See github.com/phantom567459/SoundFMVextractor (HashHelper.vb)."""
    h = 0x811C9DC5
    for b in value.encode("latin-1", "replace"):
        h ^= b | 0x20
        h = (h * 0x1000193) & 0xFFFFFFFF
    return h


# --- Known ordnance/weapon/explosion property names ----------------------
# Corrected: TurnRate, PersonScale, BuildingScale were wrong in the
# original table (each was missing/shifting a byte). Verified by
# recomputing fnv1a32() for every entry in this table.
COMMON_PROPERTY_NAMES = {
    "ImpactEffectWater": 0xECAAA59D,
    "ExplosionName": 0x9B30E763,
    "GeometryName": 0x47C86B4A,
    "TrailEffect": 0x7E71E2C6,
    "OrdnanceSound": 0xCCF9BE5C,
    "LifeSpan": 0x7C7C544B,
    "TurnRate": 0x0F77C4E8,       # fixed: was 0x000F77C4
    "Velocity": 0x32741C32,
    "Gravity": 0xD209FF45,
    "Rebound": 0x678DE338,
    "Damage": 0x59E94C40,
    "VehicleScale": 0x6CCDC5EF,
    "PersonScale": 0x0BB94602,    # fixed: was 0x00BB9460
    "DroidScale": 0x338A07CD,
    "BuildingScale": 0x02168F61,  # fixed: was 0x002168F6
    "AnimalScale": 0x9AA17A49,
    "DamageRadiusInner": 0xDED40DF6,
    "DamageRadiusOuter": 0xA23B939D,
    "Push": 0x876FFFDD,
    "PushRadiusInner": 0x96301EC5,
    "PushRadiusOuter": 0x71262EBE,
    "Shake": 0xBD8BB2F5,
    "ShakeLength": 0x25B787E5,
    "ShakeRadiusInner": 0x4954490D,
    "ShakeRadiusOuter": 0x125399A6,
    "Effect": 0x6E6E8D54,
    "WaterEffect": 0x87733801,
    "SoundProperty": 0xF350B4E5,
    "LightColor": 0x746CE73E,
    "LightRadius": 0x2776564D,
    "Base": 0x3DDC94D8,
    "ClassLabel": 0x92D04727,
    "Type": 0x5127F14D,
    "LightDuration": 0x6370A66B,
    "WaverRate": 0x6D4A8840,
    "WaverTurn": 0x129F08C1,
    # --- Weapon (wpnc) properties. Recovered by round-tripping this repo's
    # sample .odf/.wpnc pair through fnv1a32() and matching each odf
    # property, in file order, against the wpnc's unresolved 0x######## PROP
    # hashes at the same position - every one of the 77 properties matched
    # exactly, confirming both the ordering and the hash.
    "AnimationBank": 0xA7678F8F,
    "HighResGeometry": 0x8B3185DD,
    "TargetEnemy": 0xE10D2224,
    "TargetNeutral": 0x5F0AE167,
    "TargetFriendly": 0xCA7F5887,
    "TargetPerson": 0xFAE6054D,
    "TargetAnimal": 0x75C58748,
    "TargetDroid": 0x19007F1A,
    "TargetVehicle": 0x281B9338,
    "TargetBuilding": 0xC4409FF0,
    "MinRange": 0x1EF25B6C,
    "OptimalRange": 0x778F9AAE,
    "MaxRange": 0x43BCEBE6,
    "LockOnRange": 0x7F012F70,
    "LockOnAngle": 0x3935C7CA,
    "LockOffAngle": 0xC915B156,
    "LockTime": 0x448FBCE3,
    "ZoomMin": 0x1881B1A6,
    "ZoomMax": 0x2A96E8B4,
    "ZoomRate": 0x9573FB20,
    "PitchSpread": 0x8D0CB6CA,
    "YawSpread": 0xEBEFDCD7,
    "SpreadPerShot": 0x43AB2C7D,
    "SpreadRecoverRate": 0xCCEDB638,
    "SpreadThreshold": 0x2613B9B9,
    "SpreadLimit": 0x85A6B6FD,
    "StandStillSpread": 0x4C081D30,
    "StandMoveSpread": 0xF7561531,
    "CrouchStillSpread": 0x81FEC758,
    "CrouchMoveSpread": 0xFBDE8E79,
    "ProneStillSpread": 0x4A29A3CA,
    "ProneMoveSpread": 0x73608C73,
    "RoundsPerClip": 0x7BFF9EC9,
    "ReloadTime": 0x9FC1A1AD,
    "ShotDelay": 0xD9356908,
    "TriggerSingle": 0xED9D56DB,
    "MaxPressedTime": 0x2819FFA0,
    "SalvoCount": 0x9260EA63,
    "SalvoDelay": 0xD76F37DB,
    "InitialSalvoDelay": 0xC8388D9D,
    "SalvoTime": 0x4225631D,
    "OrdnanceName": 0xF8899C5E,
    "FirePointName": 0x4274FC96,
    "IconTexture": 0x8DDBEB4B,
    "ModeTexture": 0x54E9F4E5,
    "ReticuleTexture": 0x617E2F93,
    "ScopeTexture": 0x32E2A37A,
    "MuzzleFlash": 0xCFCCCBCE,
    "FlashColor": 0xEB4230AC,
    "FlashLength": 0xB7B2D011,
    "FlashLightColor": 0xF2E54D72,
    "FlashLightRadius": 0x83EEA041,
    "FlashLightDuration": 0xA3BFD787,
    "Discharge": 0x8B83F9B7,
    "ChargeRateLight": 0xF5701D6D,
    "MaxChargeStrengthLight": 0xE6C12D74,
    "ChargeDelayLight": 0x516C9F08,
    "ChargeRateHeavy": 0x60DDBF8E,
    "MaxChargeStrengthHeavy": 0xA61D280F,
    "ChargeDelayHeavy": 0xF303AC7B,
    "RecoilLengthHeavy": 0x4B92A06A,
    "RecoilStrengthHeavy": 0x12BA9F01,
    "RecoilDecayHeavy": 0xB97F1F52,
    "Firesound": 0xCFDE2FC0,
    "FireEmptySound": 0x20E1E379,
    "ReloadSound": 0xEF6119F3,
    "ChargeSound": 0x4EE58508,
    "ChargeSoundPitch": 0x12586DAE,
    "ChangeModeSound": 0xC77178C1,
    "WeaponChangeSound": 0x352A4DB8,
    "JumpSound": 0x580DC4EC,
    "LandSound": 0xAD3E89E3,
    "RollSound": 0xD211149D,
    "ProneSound": 0xCADB9D74,
    "SquatSound": 0xFC9B0358,
    "StandSound": 0x0E8B2D62,
    # --- Remaining ordc/wpnc/expc properties. Recovered not by guessing but
    # by round-tripping swbf-unmunge.exe's own decode: ran
    # `swbf-unmunge -file all.lvl -mode extract` (which recurses into every
    # nested lvl_ chunk and, for the ~115 ordc/wpnc/expc classes it fully
    # recognizes, writes a normal human-readable .odf back out), then for
    # each recovered .odf matched its Properties section, in file order,
    # against that same chunk's still-unresolved 0x######## PROP hashes and
    # confirmed fnv1a32(name) == hash for all 103 of them - 0 mismatches,
    # and it accounts for every unresolved hash found across all.lvl,
    # shell.lvl and load.lvl (checked exhaustively, none left over).
    "AimAzimuth": 0x3186FBFE,
    "AimDistance": 0xECDB9D1D,
    "AimElevation": 0x8339FB35,
    "ArmorScale": 0xA0BB60CE,
    "AutoAimSize": 0x7DBE88FA,
    "BlurLength": 0x9D0FB7A4,
    "BuildingBuild": 0x09CC7F53,
    "BuildingHealth": 0xA6C59E9F,
    "BuildingRebuild": 0x03EC6220,
    "CableLength": 0xD9B746B2,
    "ChargeUpEffect": 0x18C5F75D,
    "ChunkGeometryName": 0xDA328455,
    "ChunkOmega": 0x166F6939,
    "ChunkPhysics": 0xC8E32DC3,
    "ChunkSpeed": 0x945D1D15,
    "ChunkStartDistance": 0xCD61E401,
    "ChunkTerrainCollisions": 0x0A157BE2,
    "ChunkTrailEffect": 0x94F0544B,
    "CollisionOtherSound": 0xEDC6251A,
    "CollisionSound": 0xC28F0C96,
    "DamageRadius": 0xB68798A6,
    "Decal": 0xDE15F6AE,
    "DetatchSound": 0x5EDE4703,
    "DroidHealth": 0x9622C73B,
    "ExpireEffect": 0x028B22CF,
    "ExplosionExpire": 0x2E51081F,
    "ExplosionImpact": 0x61A8BB12,
    "ExtremeRange": 0x8474EA2C,
    "FadeOutTime": 0x798DC484,
    "FireLoopSound": 0xB53B908E,
    "ForceFireAnimation": 0x079C1D86,
    "Friction": 0xA51BE2BB,
    "GlowLength": 0xC3A8B510,
    "HealthScale": 0xC37970CF,
    "HeatPerShot": 0x67CFFD1A,
    "HeatRecoverRate": 0x57E7BBA3,
    "HeatThreshold": 0x5621511E,
    "HideOnFire": 0x5578D44C,
    "HitSound": 0xC5BDD741,
    "ImpactEffect": 0x11727A92,
    "ImpactEffectRigid": 0xA3091269,
    "ImpactEffectShield": 0x4BC6C22F,
    "ImpactEffectSoft": 0xD6220FE6,
    "ImpactEffectStatic": 0x087EA2F0,
    "ImpactEffectTerrain": 0x5A554273,
    "InitialCableLength": 0xE4C659B4,
    "KickBuildup": 0x069AD4D0,
    "KickSpread": 0xEF4935B0,
    "KickStrength": 0x16E4DB4A,
    "Label": 0xF69717FD,
    "LaserGlowColor": 0x30580110,
    "LaserLength": 0xBA858240,
    "LaserTexture": 0xB709B5E7,
    "LaserWidth": 0x6F179AB8,
    "MaxDamage": 0x47D0E794,
    "MaxItems": 0x54C3C993,
    "MaxSpread": 0xAEDCBD12,
    "MaxStrength": 0x40BD9B1C,
    "MinSpread": 0x59053068,
    "MinStrength": 0x4D832102,
    "MuzzleFlashEffect": 0x39AAA863,
    "NEXTCHARGE": 0x8316F444,
    "NumChunks": 0xF54F2869,
    "OrdnanceEffect": 0x3F30370C,
    "OverheatSound": 0xF4A517D4,
    "OverheatSoundPitch": 0xB52F4E1A,
    "OverheatStopSound": 0xA0B09F74,
    "PushRadius": 0x19B5E2BB,
    "Range": 0xFADC0CD2,
    "RecoilDecayLight": 0xA9B93C09,
    "RecoilDelayHeavy": 0xB7C581AD,
    "RecoilDelayLight": 0x7CD21CEA,
    "RecoilLengthLight": 0x903574D1,
    "RecoilStrengthLight": 0xCF3649E6,
    "RefillFromItem": 0xC7AF6C62,
    "RoundsPerSalvo": 0x168AD1D6,
    "ScatterDistance": 0xEBD81A5E,
    "SelfDestructSoundPitch": 0x4C5AA5AA,
    "ShakeRadius": 0xD634B283,
    "ShieldScale": 0xD0545C72,
    "ShotElevate": 0xAE3DB60D,
    "ShotPatternCount": 0xA4DFAFC0,
    "ShotPatternPitchYaw": 0xA4120AA8,
    "ShotsPerSalvo": 0x03B38558,
    "SniperScope": 0xF15AAEB2,
    "SpreadRecover": 0x33841A8A,
    "StickAnimal": 0xD1B49B73,
    "StickBuilding": 0x7449C0B3,
    "StickBuildingDead": 0xAD9C5FA5,
    "StickBuildingUnbuilt": 0xA8A2AFF4,
    "StickDroid": 0x3B3F45B7,
    "StickPerson": 0x01E9CAEA,
    "StickTerrain": 0x95706D30,
    "StickVehicle": 0x9B4AF9ED,
    "StrikeOrdnanceName": 0x6FD92BE8,
    "SwingTime": 0x7C9C8AE0,
    "SwitchImmediately": 0xC43B77DD,
    "Texture": 0x3C6468F4,
    "TriggerAll": 0x1C1B6976,
    "VehicleHealth": 0x14E381C9,
    "WeaponChange": 0x1ADA38C1,
    "WeaponSection": 0xD0329E80,
    "ZoomFirstPerson": 0x14EC7E1D,
    # --- entc (GameObjectClass) properties. entc turned out to use the
    # exact same BASE/TYPE/PROP wire format as ordc/wpnc/expc (confirmed by
    # hexdumping an entc chunk's raw payload), so it was mined the same way:
    # cross-referencing swbf-unmunge's own recovered .odf files (from
    # `all.lvl -mode extract`) against each entc chunk's unresolved PROP
    # hashes, in order, verifying fnv1a32(name) == hash. 263 of these
    # matched cleanly across 27 distinct entc types; a handful of
    # Value_ATK_*/Value_DEF_*/TEMP_Type properties in one file
    # (all_hover_hovernaut.odf) didn't line up positionally and were left
    # out rather than guessed - they still round-trip fine as raw 0x hashes.
    "AISCDriverGetInSound": 0xE0999151,
    "AISCDriverGetOutSound": 0x3B62B992,
    "AISCFieldFollowSound": 0x1D988641,
    "AISCFieldHoldSound": 0x8018B09D,
    "AISCFieldMoveOutSound": 0x150A38E9,
    "AISCGunnerAllClearSound": 0x22987695,
    "AISCGunnerGetInSound": 0x7EF094D0,
    "AISCGunnerGetOutSound": 0xDF526F61,
    "AISCGunnerSteadySound": 0x1AEC5F13,
    "AISCPassengerGetInSound": 0xF9D92705,
    "AISCPassengerGetOutSound": 0x45912FAE,
    "AISCPassengerMoveOutSound": 0x2512BB6D,
    "AISCPassengerStopSound": 0xA473E5EA,
    "AISCResponseNosirSound": 0x60681218,
    "AISCResponseYessirSound": 0x8E131444,
    "AISizeType": 0xAFF22806,
    "Acceleraton": 0x9F8A0040,
    "AcquiredTargetSound": 0x399EE477,
    "AddHealth": 0x3F9AB262,
    "AddSpringBody": 0x5F1B7721,
    "AimFactorMove": 0xA5F0BEE0,
    "AimFactorPostureCrouch": 0xC7898205,
    "AimFactorPostureProne": 0x13FFE717,
    "AimFactorPostureSpecial": 0x639C75CE,
    "AimFactorPostureStand": 0x497C03A9,
    "AimFactorStrafe": 0x95A83C02,
    "AimTension": 0x5E3171CC,
    "AimValue": 0xA5E1E577,
    "AimerNodeName": 0xC182ABC2,
    "AimerPitchLimits": 0x2D6487BD,
    "AimerPitchLimts": 0x0F5FD13C,
    "AimerYawLimits": 0xA9C3675C,
    "AimerYawLimts": 0xB5C7341B,
    "AllMusic": 0x4232E9BF,
    "Ambient2Sound": 0x9A108B4A,
    "AmbientSound": 0x65A920BA,
    "AnimatedPilotPosition": 0x11CE2025,
    "AnimationName": 0x98557A5E,
    "ApproachingTargetSound": 0xEB6089A1,
    "BankAngle": 0x6D7C9F7E,
    "BankFilter": 0x35DE28F1,
    "BarrelLength": 0xEF095EA9,
    "BarrelNodeName": 0x1E534B12,
    "BarrelRecoil": 0x5A29D1CB,
    "BodyOmegaXSpringFactor": 0xA8A2ECBE,
    "BodySpringLength": 0xEAFE81D0,
    "BuildingCollision": 0x9828F43F,
    "CAMERASECTION": 0xBACA6BF1,
    "CHUNKSECTION": 0xDA1DE03D,
    "CameraDistance": 0x4E2C3B35,
    "CameraHeight": 0x09205FE5,
    "CapturePosts": 0x325F7C20,
    "ChunkBounciness": 0xC2F5FFD5,
    "ChunkNodeName": 0xEBF014FD,
    "ChunkSmokeEffect": 0xD350C3DE,
    "ChunkSmokeNodeName": 0x472E0410,
    "ChunkStickiness": 0xB94AE34E,
    "ChunkTerrainEffect": 0x048F1226,
    "ChunkUpFactor": 0xC316AE10,
    "CockpitTension": 0x104CD1B8,
    "CollisionInflict": 0x2EF03732,
    "CollisionRootScale": 0xC64A53AB,
    "CollisionScale": 0x1A87BACF,
    "CollisionThreshold": 0x2CDB588A,
    "DamageAttachPoint": 0xCA698181,
    "DamageEffect": 0xAE2BFDD1,
    "DamageEffectScale": 0x57CE6C85,
    "DamageInheritVelocity": 0xD06D0B7C,
    "DamageStartPercent": 0x06182385,
    "DamageStopPercent": 0x37FE5CA9,
    "DeathSound": 0x6A9D502C,
    "Deceleration": 0x8B12C806,
    "DropItemClass": 0x406D2B75,
    "DropItemProbability": 0x26996DAE,
    "DropShadowSize": 0x82AF5399,
    "EngineSound": 0x84BBEF6A,
    "ExplosionDeath": 0xA2723442,
    "ExplosionDestruct": 0x8A323F32,
    "ExplosionTrigger": 0x6A5E371C,
    "EyePointCenter": 0x2D1420E5,
    "EyePointOffset": 0x41568C97,
    "FinAnimation": 0xEF563A98,
    "FirstPerson": 0x6363D774,
    "FirstPersonFOV": 0x4785D6B7,
    "FleeSound": 0x94353BF8,
    "FlyerSection": 0x4CBE283C,
    "FoleyFXClass": 0x99D55922,
    "FoleyFXGroup": 0xA1C9CBC5,
    "FootBoneLeft": 0xD382EDB2,
    "FootBoneRight": 0xA2F4825F,
    "FootWaterSplashEffect": 0x057266EE,
    "FootstepSound1": 0x98157C89,
    "FootstepSound2": 0x951577D0,
    "ForceMode": 0x9D1F5707,
    "ForwardSpeed": 0xD42834BD,
    "ForwardTurnSpeed": 0x7EAB3C84,
    "GeometryLowRes": 0xA490EBA1,
    "GravityScale": 0x97F2E0F1,
    "HealthTexture": 0x0DB7468E,
    "HealthType": 0xE8821677,
    "HeardEnemySound": 0xD3C54780,
    "HidingSound": 0xC007B675,
    "HierarchyLevel": 0x407E801E,
    "HurtSound": 0x72981AF7,
    "IgnorableCollsion": 0x4AEE3E6B,
    "ImpMusic": 0xCEDC7E88,
    "IsPilotExposed": 0x2B60E217,
    "LandedHeight": 0x7642977A,
    "LandingSpeed": 0x6AE3A957,
    "LandingTime": 0x1283209D,
    "LegBoneLeft": 0x03FD0638,
    "LegBoneRight": 0xC42BE48D,
    "LegBoneTopLeft": 0x580F4D75,
    "LegBoneTopRight": 0x9EB9FC8E,
    "LegPairCount": 0x153289AE,
    "LevelDamp": 0x252DFAAB,
    "LevelFilter": 0xF4F5F38F,
    "LevelSpring": 0x02E46404,
    "LiftDamp": 0xA37B2EC6,
    "LiftSpring": 0x1B5071F9,
    "LowHealthSound": 0xF8D3E5D2,
    "LowHealthThreshold": 0xC865523E,
    "MapScale": 0x0E8C3FE5,
    "MapTexture": 0x9DC38D80,
    "MaxHealth": 0x19971F1B,
    "MaxSpeed": 0x86DF0364,
    "MaxStrafeSpeed": 0x3437A33D,
    "MaxTurnSpeed": 0x5433F589,
    "MidSpeed": 0x838BDA20,
    "MinSpeed": 0xFBE2EB62,
    "MoveTension": 0x21050BF6,
    "MoveTensionX": 0x88F1308A,
    "MoveTensionY": 0x89F1321D,
    "MoveTensionZ": 0x86F12D64,
    "MovingTurnOnly": 0x3C45420E,
    "MusicDelay": 0x1784E0A3,
    "MusicSpeed": 0x4DEFB65F,
    "NextAimer": 0x665D96EA,
    "NextBarrel": 0xE98F377C,
    "NextDropItem": 0x5EA04066,
    "NoCombatInterrupt": 0x28ADDA49,
    "NoDeathExplosions": 0x16181B74,
    "NoEnterVehicles": 0x939B9985,
    "NormalDirection": 0xDE649A87,
    "OmegaXDamp": 0x9D59000A,
    "OmegaXSpring": 0x9184393D,
    "OmegaZDamp": 0xAA764468,
    "OmegaZSpring": 0x37949663,
    "Ordnancecollision": 0xFB2BDF07,
    "OverrideTexture": 0x09DB74AC,
    "OverrideTexture2": 0x227894BA,
    "PCPitchRate": 0x841BFEB8,
    "PCSpinRate": 0x7C21A3B6,
    "PCTurnRate": 0x3C11E4C1,
    "PPitchRate": 0xFA5D55A1,
    "PassengerEyePoint": 0xBFBBAB62,
    "PassengerSlots": 0x64C511A4,
    "PilotAnimation": 0x6E4FC069,
    "PilotPosition": 0x51CA39A6,
    "PilotSkillRepairScale": 0x0842B6A7,
    "PilotType": 0x91EE6929,
    "PitchDamp": 0xFEABA02F,
    "PitchFilter": 0x9B0CA8C3,
    "PitchLimits": 0x3403B139,
    "PitchRate": 0xB9B8ABC3,
    "PitchTurnFactor": 0x0B2FC2F9,
    "PreparingForDamageSound": 0x130971AE,
    "ReverseSpeed": 0x1F069EC0,
    "SCDriverGetInSound": 0x47041BAF,
    "SCDriverGetOutSound": 0x4EFE8D94,
    "SCFieldFollowSound": 0x491B3917,
    "SCFieldHoldSound": 0xB2A1A19F,
    "SCFieldMoveOutSound": 0xE0E7AA4F,
    "SCGunnerAllClearSound": 0xA3D45943,
    "SCGunnerGetInSound": 0xD1F9B472,
    "SCGunnerGetOutSound": 0xC27C316F,
    "SCGunnerSteadySound": 0x981F3D5D,
    "SCPassengerGetInSound": 0x6C89065F,
    "SCPassengerGetOutSound": 0xF260F2A4,
    "SCPassengerMoveOutSound": 0x5B7C85CB,
    "SCPassengerStopSound": 0x5D42F4C8,
    "SCResponseNosirSound": 0xC989ADE6,
    "SCResponseYessirSound": 0x765BE0F2,
    "ScanningRange": 0x465909FB,
    "SetAltitude": 0x4F358485,
    "SkeletonLowRes": 0x55F297D2,
    "SkeletonName": 0x7012F6CD,
    "SkeletonRootScale": 0x4CACC3D0,
    "SoldierCollision": 0x5DFDC07F,
    "SpawnPointCount": 0x32579F4D,
    "SpawnPointLocation": 0xA067D145,
    "SpinRate": 0xDDD0E74B,
    "StatusTexture": 0x037CD46E,
    "StompDecal": 0x9B650367,
    "StompDecalSize": 0xE611ED7E,
    "StompThreshold": 0x4F08E1F5,
    "StoppedTurnSpeed": 0x7C8F02C0,
    "StrafeRollAngle": 0xF1A4896C,
    "StrafeSpeed": 0xFE1CE511,
    "TakeoffHeight": 0x5107FAFC,
    "TakeoffSound": 0xC093B232,
    "TakeoffSpeed": 0x8255424E,
    "TakeoffTime": 0x61195C0A,
    "TerrainCollision": 0xAFE693CE,
    "TerrainLeft": 0x21322ADB,
    "TerrainRight": 0xB62DEB24,
    "ThirdPersonFOV": 0x20728C12,
    "ThrustAttachOffset": 0x34775769,
    "ThrustAttachPoint": 0x2E0557F0,
    "ThrustEffect": 0x0894D756,
    "ThrustEffectMaxScale": 0xC3FD784A,
    "ThrustEffectMinScale": 0xF174A3F8,
    "ThrustEffectScaleStart": 0xE97041B8,
    "ThrustPitchAngle": 0x4C5B08A6,
    "TickSound": 0xACFE0D9B,
    "TickSoundPitch": 0x18953967,
    "TiltValue": 0x359D5227,
    "TrackCenter": 0xE85D5895,
    "TrackOffset": 0xFD3D9507,
    "Traction": 0x4FE8D3F1,
    "TrakCenter": 0x1081C71A,
    "TransmitRange": 0xEB460A76,
    "TurnFilter": 0xDE721CB4,
    "TurnOffSound": 0x042DABAE,
    "TurnOffTime": 0xC8A9E3C6,
    "TurnOnSound": 0xEA603E52,
    "TurnThreshold": 0x46BBA6D5,
    "TurningOffSound": 0x7D2C2FA2,
    "TurretActivateSound": 0xE9D0FB69,
    "TurretDeactivateSound": 0xF84AA204,
    "TurretNodeName": 0xB69A7CB4,
    "TurretPitchSound": 0x28F016D2,
    "TurretPitchSoundPitch": 0x4ED7A4D0,
    "TurretYawSound": 0x1FA43B99,
    "TurretYawSoundPitch": 0x1503F425,
    "UnitType": 0x54A7AEDF,
    "ValueBleed": 0x4EBF97DA,
    "VehicleCollisionSound": 0x84824518,
    "VehiclePosition": 0xDA1B8C0C,
    "VehicleType": 0x5233B817,
    "VelocityDamp": 0x661D8524,
    "VelocitySpring": 0xDF0D2C6F,
    "WALKERSECTION": 0x1CD9F762,
    "WakeEffect": 0x26D7A21C,
    "WakeWaterSplashEffect": 0xF44FEE52,
    "WalkerLegPair": 0x2AEA4ADD,
    "WaterSplashEffect": 0xDEA64CFA,
    "WeaponAmmo": 0xDC19BD13,
    "WeaponAmmo1": 0x9E84BC86,
    "WeaponAmmo2": 0x9D84BAF3,
    "WeaponAmmo3": 0x9C84B960,
    "WeaponAmmo4": 0xA384C465,
    "WeaponChannel": 0x16472328,
    "WeaponChannel3": 0x2CFC4381,
    "WeaponChannel4": 0x2DFC4514,
    "WeaponName": 0xFBF47DBA,
    "WeaponName1": 0x2CE1A1D1,
    "WeaponName2": 0x29E19D18,
    "WeaponName3": 0x2AE19EAB,
    "WeaponName4": 0x2FE1A68A,
    "YawLimits": 0x2C3E8078,
    "acceleration": 0x2FA0BA9D,
    "vehiclecollision": 0xDE5365A1,
    # --- entc (object/prop/light) properties. Found by hashing likely names
    # and matching the unnamed hashes in the stock game; every one also fits
    # the values it's used with (Color = '0 0 255' on the light classes,
    # AttachToHardPoint = 'hp_light_a', ...).
    "Animation": 0xE145EE5D,
    "AttachEffect": 0x6A6C7E0D,
    "AttachOdf": 0xA9D0D48B,
    "AttachToHardPoint": 0x3BE7B80A,
    "AttachTrigger": 0x72306628,
    "ChunkFrequency": 0x43B50014,
    "Color": 0x3D7E6258,
    "ConeAngle": 0xA3F03B49,
    "ConeFadeLength": 0x2C52F680,
    "ConeHeight": 0xEAAAA7A9,
    "ConeLength": 0x9342844E,
    "ConeWidth": 0x6F7F1016,
    "DestroyedGeometryName": 0x5DC6E917,
    "Emitter": 0x576B09CD,
    "ExplosionOffset": 0xC5637083,
    "FlareAngle": 0x5AE03A9E,
    "FlareIntensity": 0x633ED9D4,
    "FlickerPeriod": 0xD92EA25E,
    "FlickerType": 0x3685BB77,
    "GeometryColorMax": 0x35B1CFC2,
    "GeometryColorMin": 0x3F9CC4C8,
    "Height": 0xD5BDBB42,
    "HeightScale": 0xCAAA3778,
    "IdleAnimation": 0x297BC6EB,
    "IdleDelay": 0xFF701082,
    "IdleRotateSpeed": 0xEDE68C8B,
    "MaxAlpha": 0xB1E439AF,
    "MaxDelayLight": 0xE2742820,
    "MaxDistance": 0xE6915CF6,
    "MaxLifetime": 0x9D399B92,
    "MaxLight": 0x569C3E43,
    "MaxSize": 0x80BA1308,
    "MinAlpha": 0x7DB59929,
    "MinDelayLight": 0xE1C681CA,
    "MinDistance": 0xB835CF50,
    "MinLifetime": 0xBD9397EC,
    "MinLight": 0x5A4042B1,
    "MinSize": 0xEAEBCF6E,
    "Radius": 0x0DBA4CB3,
    "RadiusFadeMax": 0x6E74D661,
    "RadiusFadeMin": 0x80887A6F,
    "SoundName": 0x7A5FF18F,
    "SpreadRadius": 0x82A0379C,
    "Static": 0xD290C23B,
    "TargetableCollision": 0xD23DBAF8,
}
HASH_TO_NAME = {v: k for k, v in COMMON_PROPERTY_NAMES.items()}

_HEX_KEY_RE = re.compile(r"^0x[0-9a-fA-F]{8}$")


def hash_to_key(hash_id: int) -> str:
    """Binary -> display string. Unknown hashes become '0x########'."""
    return HASH_TO_NAME.get(hash_id, f"0x{hash_id:08x}")


def key_to_hash(key: str) -> int:
    """Display string -> binary hash.

    THE FIX: if `key` looks like a raw '0x########' hash (i.e. it came from
    hash_to_key() and was never given a real name), parse it back as the
    original integer instead of hashing the literal text. Only real names
    get run through fnv1a32.
    """
    key = key.strip()
    if _HEX_KEY_RE.match(key):
        return int(key, 16)
    return fnv1a32(key)


# --- Chunk type <-> file extension (for labeling / import-export only;
#     never used to decide how bytes are parsed or re-tagged) -------------

FILE_TYPE_EXTENSIONS = {
    "mcfg": ".config", "snd_": ".config", "zaf_": ".zafbin", "zaa_": ".zaabin",
    "lvl_": ".lvl", "Locl": ".loc", "tex_": ".texture", "LuaP": ".script",
    "scr_": ".script", "odf_": ".odf", "ODF_": ".odf", "ordc": ".ord",
    "wpnc": ".wpn", "expc": ".exp", "SHDR": ".shader", "entc": ".odf",
    "skel": ".model", "ANIM": ".anims", "bnd_": ".boundary", "plan": ".congraph",
    "fx__": ".envfx", "lght": ".light", "wrld": ".world",
}
EXTENSION_TO_TAG = {v.lstrip("."): k for k, v in FILE_TYPE_EXTENSIONS.items()}
# Chunk types whose payload is BASE+TYPE+repeated-PROP (the same
# key/value-with-hashed-names wire format as ordc/wpnc/expc - "entc" was
# confirmed to match byte-for-byte by hexdumping an entc chunk's payload,
# see decode_ordnance_payload/encode_ordnance_payload below).
KV_TAGS = {"ordc", "wpnc", "expc", "entc"}
# Chunk types whose payload is known to start with a NAME sub-block,
# based on what _find_name_from_chunk actually searches for.
NAME_WRAPPED_TAGS_DEFAULT = True  # everything except KV_TAGS + plan (see pack_new_chunk)


def _pad_len(pos: int) -> int:
    return (-pos) % 4


def tagged_block(tag: bytes, payload: bytes) -> bytes:
    padded = payload + (b"\x00" * _pad_len(len(payload)))
    return tag + struct.pack("<I", len(payload)) + padded


def decode_ascii(data: bytes) -> str:
    if not data:
        return ""
    return data.rstrip(b"\x00").decode("ascii", errors="replace").strip()


def sanitize_name(value: str) -> str:
    cleaned = value.replace("\x00", "").replace("/", "_").replace("\\", "_")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch in "._-")
    return cleaned.strip().strip(".")


def matches_filter(chunk: "Chunk", tag_q: str = "", name_q: str = "", data_q: str = "",
                   name: Optional[str] = None) -> bool:
    """Case-insensitive 'contains' filter shared by the IDE's per-tab find
    bar and its cross-file Find in Files dialog, so both search exactly the
    same way. Every non-empty query must match; an empty one is ignored.
    `name` lets a caller that already computed display_name() (e.g. while
    building a tree) pass it in instead of recomputing it here."""
    tag_q, name_q, data_q = tag_q.strip().lower(), name_q.strip().lower(), data_q.strip().lower()
    if not (tag_q or name_q or data_q):
        return True
    if tag_q and tag_q not in chunk.tag.lower():
        return False
    if name_q and name_q not in (name if name is not None else chunk.display_name()).lower():
        return False
    if data_q and data_q.encode("utf-8", "replace") not in chunk.payload.lower():
        return False
    return True


def find_in_container(container: "Container", tag_q: str = "", name_q: str = "", data_q: str = "",
                      _idx_path: tuple = (), _name_path: tuple = ()):
    """Recursively walks `container` (including every nested 'lvl_' chunk)
    yielding (index_path, index, chunk, name_path) for each chunk matching
    matches_filter(). index_path is the chain of chunk indices from the
    container's root down to (but not including) the match - empty for a
    top-level match - and is exactly what's needed to navigate back to it
    later (chunk.display_name() alone isn't a reliable path component:
    ordc/wpnc/expc/entc chunks have no NAME block at all). name_path is the
    same chain as display names, for showing a human-readable location."""
    for i, chunk in enumerate(container.body.chunks):
        name = chunk.display_name()
        if matches_filter(chunk, tag_q, name_q, data_q, name=name):
            yield (_idx_path, i, chunk, _name_path)
        if chunk.tag == "lvl_":
            yield from find_in_container(
                chunk.get_nested_container(), tag_q, name_q, data_q,
                _idx_path + (i,), _name_path + (name or "lvl_",))


# --- Ordnance/weapon/explosion payload codec ------------------------------
# ordc/wpnc/expc payloads are: BASE block, TYPE block, then zero or more
# PROP blocks (hash + null-terminated ascii value). No NAME wrapper.


def decode_ordnance_payload(payload: bytes) -> dict:
    pos = 0
    base, type_name = "", ""
    props: list[tuple[str, str]] = []
    while pos + 8 <= len(payload):
        tag = payload[pos:pos + 4]
        length = struct.unpack_from("<I", payload, pos + 4)[0]
        block = payload[pos + 8:pos + 8 + length]
        if tag == b"BASE":
            base = decode_ascii(block)
        elif tag == b"TYPE":
            type_name = decode_ascii(block)
        elif tag == b"PROP":
            if len(block) >= 4:
                hash_id = struct.unpack_from("<I", block, 0)[0]
                value = decode_ascii(block[4:])
                props.append((hash_to_key(hash_id), value))
        pos += 8 + length + _pad_len(length)
        if pos > len(payload):
            break
    return {"base": base, "type": type_name, "props": props}


def encode_ordnance_payload(base: str, type_name: str, props: list[tuple[str, str]]) -> bytes:
    blocks = [
        tagged_block(b"BASE", (base or "").encode("ascii", "replace") + b"\x00"),
        tagged_block(b"TYPE", (type_name or "").encode("ascii", "replace") + b"\x00"),
    ]
    for key, value in props:
        value_bytes = str(value).encode("ascii", "replace") + b"\x00"
        prop_payload = struct.pack("<I", key_to_hash(key)) + value_bytes
        blocks.append(tagged_block(b"PROP", prop_payload))
    return b"".join(blocks)


# --- ordc/wpnc/expc/entc <-> real .odf text ---------------------------------
# The munged BASE/TYPE/PROP payload above is a flat, order-preserving list -
# fine for the property-table editor, tedious for bulk edits. This gives the
# same chunk a real, editable .odf text form so an external editor (or a
# whole file's worth of community-doc snippets) can be pasted in directly.
#
# Section header and quoting conventions verified against swbf-unmunge's own
# recovered .odf output (see COMMON_PROPERTY_NAMES's docstring comment for
# how that reference set was built): a bare number (one whitespace-
# separated token that parses as a number) is written unquoted; anything
# else - including a space-separated list of numbers, e.g. a color "255 198
# 62" - is quoted. TYPE isn't a real .odf field (it's the compiled chunk's
# own identifier, normally implied by the filename you compile) - it's
# preserved in a leading comment instead, so it survives a file rename.

KV_CLASS_SECTION = {"ordc": "OrdnanceClass", "wpnc": "WeaponClass",
                    "expc": "ExplosionClass", "entc": "GameObjectClass"}
_ODF_TYPE_COMMENT_RE = re.compile(r"^//.*\bTYPE\s*=\s*(\S+)", re.IGNORECASE)
_ODF_KV_RE = re.compile(r"^([A-Za-z0-9_]+)\s*=\s*(.*)$")


def _format_odf_value(value: str) -> str:
    tokens = value.split()
    if len(tokens) == 1:
        try:
            float(tokens[0])
        except ValueError:
            pass
        else:
            return tokens[0]
    return f'"{value}"'


def _parse_odf_value(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1]
    return raw


def encode_kv_chunk_to_odf_text(tag: str, payload: bytes) -> str:
    info = decode_ordnance_payload(payload)
    section = KV_CLASS_SECTION.get(tag, "Properties")
    lines = [
        f"// TYPE = {info['type']}  (this chunk's own identifier - keep this line so "
        "re-importing this file restores it even if you rename the file)",
        f"[{section}]", "",
        f"ClassLabel = {_format_odf_value(info['base'])}", "",
        "[Properties]", "",
    ]
    for key, value in info["props"]:
        lines.append(f"{key} = {_format_odf_value(value)}")
    return "\n".join(lines) + "\n"


def decode_odf_text_to_kv_payload(text: str, fallback_type: str = "") -> bytes:
    base, type_name = "", fallback_type
    props: list[tuple[str, str]] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _ODF_TYPE_COMMENT_RE.match(line)
        if m:
            type_name = m.group(1)
            continue
        if line.startswith("//") or line.startswith("["):
            continue
        m = _ODF_KV_RE.match(line)
        if not m:
            continue
        key, value = m.group(1), _parse_odf_value(m.group(2))
        if key.lower() == "classlabel":
            base = value
        else:
            props.append((key, value))
    return encode_ordnance_payload(base, type_name, props)


# --- Chunk / Body / Container object graph --------------------------------
# This is the core fix for lossless round-tripping: every chunk keeps its
# ORIGINAL raw payload bytes by default. Nothing is ever re-serialized
# unless something inside it was actually edited (dirty propagation via
# notify_changed), and container-level prefix/trailing bytes are captured
# explicitly instead of silently dropped.


class Chunk:
    def __init__(self, tag: str, payload: bytes, pad: bytes = b""):
        self.tag = tag
        self.payload = payload
        self.pad = pad
        self.parent_body: Optional["Body"] = None
        self._nested: Optional["Container"] = None
        self.dirty = False

    def raw_chunk_bytes(self) -> bytes:
        return self.tag.encode("ascii", "replace") + struct.pack("<I", len(self.payload)) + self.payload

    def set_payload(self, new_payload: bytes) -> None:
        self.payload = new_payload
        self._nested = None  # stale; will be reparsed lazily if opened again
        self.dirty = True
        self._notify()

    def _notify(self) -> None:
        if self.parent_body is not None:
            self.parent_body.notify_changed()

    def get_nested_container(self) -> Optional["Container"]:
        """Only meaningful for 'lvl_' chunks. Lazily parses the payload as
        its own UCFB-style container so it can be browsed/edited in place.
        """
        if self.tag != "lvl_":
            return None
        if self._nested is None:
            header = self._lvl_header()
            if header is not None:
                # 8-byte [name hash][size of the rest] header. It must NOT be
                # scanned as a chunk header: the hash is arbitrary bytes and
                # can happen to look like a printable tag + plausible length
                # (e.g. 'wY-6'), which used to swallow the whole level as one
                # bogus chunk. Also keeps the size field correct on rebuild.
                inner = parse_body(self.payload[8:])
                inner.len_header = header
                inner.trailing, inner.inner_trailing = b"", inner.trailing
                self._nested = Container(False, inner)
            else:
                self._nested = parse_container(self.payload)
            self._nested.owner_chunk = self
        return self._nested

    def _lvl_header(self) -> Optional[bytes]:
        p = self.payload
        if len(p) >= 8 and struct.unpack_from("<I", p, 4)[0] == len(p) - 8:
            return p[:8]
        return None

    def display_name(self) -> str:
        if self.tag == "lvl_":
            name = self._named_by_chunk()
            if name:
                return name
            header = self._lvl_header()
            return f"0x{struct.unpack_from('<I', header, 0)[0]:08x}" if header else ""
        return self._named_by_chunk()

    def _named_by_chunk(self) -> str:
        if self.tag == "plan":
            return "(congraph)"
        if self.tag in CONFIG_TEXT_TAGS:  # NAME holds a hash, not text
            return config_file_name(self.payload)
        search_tag = b"TYPE" if self.tag == "entc" else b"NAME"
        window = self.payload[:96]
        pos = window.find(search_tag)
        if pos == -1 or pos + 8 > len(self.payload):
            return ""
        name_len = struct.unpack_from("<I", self.payload, pos + 4)[0]
        if name_len <= 0 or name_len > 256:
            return ""
        raw = self.payload[pos + 8: pos + 8 + name_len]
        if b"\x00" in raw:
            raw = raw.split(b"\x00", 1)[0]
        return decode_ascii(raw)

    def extension(self) -> str:
        return FILE_TYPE_EXTENSIONS.get(self.tag, f".{self.tag.strip() or 'bin'}")


class Body:
    """A flat run of chunks, e.g. everything inside a ucfb container, or
    inside a single 'lvl_' chunk's payload."""

    def __init__(self, prefix: bytes, chunks: list[Chunk], trailing: bytes = b""):
        self.prefix = prefix
        self.chunks = chunks
        self.trailing = trailing
        self.owner_container: Optional["Container"] = None
        # Set only for a nested 'lvl_' body whose first 8 bytes are
        # [name hash][u32 = size of everything after]; the size is recomputed
        # (along with inner_trailing, which it covers) whenever this rebuilds.
        self.len_header: Optional[bytes] = None
        self.inner_trailing = b""
        for c in chunks:
            c.parent_body = self

    def core_bytes(self) -> bytes:
        parts = [self.prefix]
        for c in self.chunks:
            parts.append(c.raw_chunk_bytes())
            parts.append(c.pad)
        rest = b"".join(parts)
        if self.len_header is not None:
            rest += self.inner_trailing
            return self.len_header[:4] + struct.pack("<I", len(rest)) + rest
        return rest

    def add_chunk(self, chunk: Chunk, index: Optional[int] = None) -> None:
        chunk.parent_body = self
        if index is None:
            self.chunks.append(chunk)
        else:
            self.chunks.insert(index, chunk)
        self.notify_changed()

    def remove_chunk(self, chunk: Chunk) -> None:
        self.chunks.remove(chunk)
        chunk.parent_body = None
        self.notify_changed()

    def move_chunk(self, chunk: Chunk, delta: int) -> int:
        """Moves `chunk` earlier (delta<0) or later (delta>0) in the chunk
        order. Order matters for the actual on-disk layout (reordering
        genuinely changes the rebuilt bytes, unlike editing a payload in
        place), so this always marks the body dirty. Returns the chunk's
        new index, clamped to stay within bounds.
        """
        old_index = self.chunks.index(chunk)
        new_index = old_index + delta
        if new_index < 0 or new_index >= len(self.chunks):
            return old_index
        self.chunks.pop(old_index)
        self.chunks.insert(new_index, chunk)
        self.notify_changed()
        return new_index

    def notify_changed(self) -> None:
        if self.owner_container is not None and self.owner_container.owner_chunk is not None:
            owner = self.owner_container.owner_chunk
            core = self.core_bytes()
            if self.owner_container.has_magic:
                new_payload = MAGIC + struct.pack("<I", len(core)) + core + self.trailing
            else:
                new_payload = core + self.trailing
            owner.payload = new_payload
            owner.dirty = True
            owner._notify()


class Container:
    """Either a whole file (has_magic=True, starts with 'ucfb') or a bare
    chunk stream with no magic header (has_magic=False) - both a top-level
    .lvl file and a single extracted chunk file fit this model."""

    def __init__(self, has_magic: bool, body: Body):
        self.has_magic = has_magic
        self.body = body
        self.owner_chunk: Optional[Chunk] = None
        body.owner_container = self

    def raw_bytes(self) -> bytes:
        core = self.body.core_bytes()
        if self.has_magic:
            return MAGIC + struct.pack("<I", len(core)) + core + self.body.trailing
        return core + self.body.trailing


def _looks_like_chunk_header(data: bytes, offset: int) -> bool:
    """Generic structural check: 4 bytes that decode as ascii, followed by
    a length field whose declared chunk fits within the remaining data.
    Deliberately NOT limited to a hardcoded tag allow-list (that was the
    root cause of bug #6 - unlisted-but-legitimate tags being misparsed).
    """
    if offset + 8 > len(data):
        return False
    tag = data[offset:offset + 4]
    try:
        tag.decode("ascii")
    except UnicodeDecodeError:
        return False
    if not all(32 <= b < 127 for b in tag):
        return False
    length = struct.unpack_from("<I", data, offset + 4)[0]
    return 0 <= length <= len(data) - offset - 8


def _find_body_start(data: bytes, max_scan: int = 128) -> int:
    if _looks_like_chunk_header(data, 0):
        return 0
    limit = min(max_scan, max(0, len(data) - 8))
    for offset in range(1, limit + 1):
        if _looks_like_chunk_header(data, offset):
            return offset
    return 0


def parse_body(data: bytes) -> Body:
    start = _find_body_start(data)
    prefix = data[:start]
    chunks: list[Chunk] = []
    pos = start
    n = len(data)
    while pos + 8 <= n:
        tag_bytes = data[pos:pos + 4]
        try:
            tag = tag_bytes.decode("ascii")
        except UnicodeDecodeError:
            break
        length = struct.unpack_from("<I", data, pos + 4)[0]
        total = 8 + length
        if total < 8 or pos + total > n:
            break  # remaining bytes captured as `trailing` below - not lost
        payload = data[pos + 8:pos + total]
        pos += total
        pad_n = min(_pad_len(pos), n - pos)
        pad = data[pos:pos + pad_n]
        pos += pad_n
        chunks.append(Chunk(tag, payload, pad))
    trailing = data[pos:]
    return Body(prefix, chunks, trailing)


def parse_container(data: bytes) -> Container:
    if data[:4] == MAGIC and len(data) >= 8:
        body_len = struct.unpack_from("<I", data, 4)[0]
        body_end = min(len(data), 8 + body_len)
        inner = parse_body(data[8:body_end])
        outer_trailing = data[body_end:]
        if outer_trailing:
            inner.trailing = inner.trailing + outer_trailing
        return Container(True, inner)
    return Container(False, parse_body(data))


def pack_new_chunk(tag: str, name: str, body_payload: bytes) -> Chunk:
    """Builds a brand new chunk from scratch (used by the IDE's "Add
    Chunk" feature - previously dead code (`_pack_chunk`) that existed but
    was never actually called from the rebuild path).
    """
    if tag in KV_TAGS or tag == "plan":
        payload = body_payload
    else:
        name_bytes = sanitize_name(name).encode("ascii", "replace") or b"new"
        name_block = tagged_block(b"NAME", name_bytes + b"\x00")
        payload = name_block + body_payload
    return Chunk(tag, payload)


# --- Texture ("tex_") chunk codec ------------------------------------------
# Layout (found by exploding a tex_ chunk with swbf-unmunge.exe -mode explode
# and cross-checking the raw payload bytes against it). Every sub-block uses
# the exact same tag+u32 length+payload framing as top-level chunks, so it's
# walked with the same parse_body()/tagged_block() used everywhere else:
#
#   NAME  - null-terminated texture name
#   INFO  - u32 format_count, 4s primary_fourcc            (informational)
#   FMT_  (one per stored pixel format; first is the one actually used)
#     INFO  - 4s fourcc, u16 width, u16 height, u16 depth, u16 mip_count,
#             u16 kind, u16 reserved
#     FACE  (one per cubemap face; a plain 2D texture has exactly one)
#       LVL_  (one per mip level, largest/mip-0 first)
#         INFO - u32 mip_index, u32 byte_size
#         BODY - raw pixel bytes for that level
#
# Verified two ways against swbf-unmunge.exe on this repo's modded_lvl/load.lvl:
#  1. `-imgfmt dds` output's 128-byte DDS header decodes to the exact same
#     fourcc/width/height/mip-count found in each tex_ chunk's FMT_/INFO,
#     and the DDS pixel data is byte-identical to the tex_ chunk's BODY.
#  2. `-imgfmt png` output, compared pixel-by-pixel against this module's
#     own BC1/BC2/BC3 decode below, differs by at most +-1 per channel on a
#     handful of pixels (a documented interpolation-rounding ambiguity in
#     the S3TC spec, not a structural bug).

DXT_FOURCCS = {"DXT1", "DXT2", "DXT3", "DXT4", "DXT5"}


def _unpack_565(value: int) -> tuple[int, int, int]:
    r = (value >> 11) & 0x1F
    g = (value >> 5) & 0x3F
    b = value & 0x1F
    r = (r << 3) | (r >> 2)
    g = (g << 2) | (g >> 4)
    b = (b << 3) | (b >> 2)
    return r, g, b


def _quantize_565(r: int, g: int, b: int) -> int:
    r5 = min(31, (r * 31 + 127) // 255)
    g6 = min(63, (g * 63 + 127) // 255)
    b5 = min(31, (b * 31 + 127) // 255)
    return (r5 << 11) | (g6 << 5) | b5


def _bc1_palette(color0: int, color1: int, punch_through: bool) -> list[tuple[int, int, int, int]]:
    """Computes the 4 RGBA palette entries for a BC1-family color block
    (the [c0, c1, interpolated-or-averaged, interpolated-or-transparent]
    tuple that the 2-bit per-texel indices select into). `punch_through`
    selects whether color0<=color1 means "3 colors + transparent" (BC1) or
    just "3 colors, no transparency" (color part of BC2/BC3, where alpha is
    stored separately)."""
    c0, c1 = _unpack_565(color0), _unpack_565(color1)
    if color0 > color1 or not punch_through:
        c2 = tuple((2 * c0[i] + c1[i] + 1) // 3 for i in range(3)) + (255,)
        c3 = tuple((c0[i] + 2 * c1[i] + 1) // 3 for i in range(3)) + (255,)
    else:
        c2 = tuple((c0[i] + c1[i] + 1) // 2 for i in range(3)) + (255,)
        c3 = (0, 0, 0, 0)
    return [c0 + (255,), c1 + (255,), c2, c3]


def _decode_bc1_color_block(block: bytes, punch_through: bool) -> list[tuple[int, int, int, int]]:
    """Decodes an 8-byte BC1 color block to 16 RGBA texels."""
    color0, color1 = struct.unpack_from("<HH", block, 0)
    indices = struct.unpack_from("<I", block, 4)[0]
    palette = _bc1_palette(color0, color1, punch_through)
    return [palette[(indices >> (2 * i)) & 0x3] for i in range(16)]


def _iter_blocks(width: int, height: int):
    blocks_x = max(1, (width + 3) // 4)
    blocks_y = max(1, (height + 3) // 4)
    for by in range(blocks_y):
        for bx in range(blocks_x):
            yield bx, by, blocks_x


def decode_bc1(data: bytes, width: int, height: int) -> bytes:
    out = bytearray(width * height * 4)
    for bx, by, blocks_x in _iter_blocks(width, height):
        off = (by * blocks_x + bx) * 8
        texels = _decode_bc1_color_block(data[off:off + 8], punch_through=True)
        _blit_block(out, texels, bx, by, width, height)
    return bytes(out)


def decode_bc2(data: bytes, width: int, height: int) -> bytes:
    out = bytearray(width * height * 4)
    for bx, by, blocks_x in _iter_blocks(width, height):
        off = (by * blocks_x + bx) * 16
        block = data[off:off + 16]
        alpha_words = struct.unpack_from("<4H", block, 0)
        alphas = []
        for word in alpha_words:
            for shift in range(0, 16, 4):
                a4 = (word >> shift) & 0xF
                alphas.append((a4 << 4) | a4)
        texels = _decode_bc1_color_block(block[8:16], punch_through=False)
        texels = [texels[i][:3] + (alphas[i],) for i in range(16)]
        _blit_block(out, texels, bx, by, width, height)
    return bytes(out)


def decode_bc3(data: bytes, width: int, height: int) -> bytes:
    out = bytearray(width * height * 4)
    for bx, by, blocks_x in _iter_blocks(width, height):
        off = (by * blocks_x + bx) * 16
        block = data[off:off + 16]
        a0, a1 = block[0], block[1]
        abits = int.from_bytes(block[2:8], "little")
        alpha_lut = [a0, a1]
        if a0 > a1:
            alpha_lut += [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
        else:
            alpha_lut += [((5 - i) * a0 + i * a1) // 5 for i in range(1, 5)]
            alpha_lut += [0, 255]
        texels = _decode_bc1_color_block(block[8:16], punch_through=False)
        texels = [texels[i][:3] + (alpha_lut[(abits >> (3 * i)) & 0x7],) for i in range(16)]
        _blit_block(out, texels, bx, by, width, height)
    return bytes(out)


def _blit_block(out: bytearray, texels, bx: int, by: int, width: int, height: int) -> None:
    for ty in range(4):
        py = by * 4 + ty
        if py >= height:
            break
        for tx in range(4):
            px = bx * 4 + tx
            if px >= width:
                break
            o = (py * width + px) * 4
            out[o:o + 4] = bytes(texels[ty * 4 + tx])


def decode_dxt_to_rgba(fourcc: str, data: bytes, width: int, height: int) -> Optional[bytes]:
    """Best-effort decode of one mip level's raw bytes to tightly packed
    RGBA8888. Returns None for a fourcc this module doesn't decode (caller
    should fall back to exporting the raw/DDS bytes instead)."""
    if fourcc == "DXT1":
        return decode_bc1(data, width, height)
    if fourcc in ("DXT2", "DXT3"):
        return decode_bc2(data, width, height)
    if fourcc in ("DXT4", "DXT5"):
        return decode_bc3(data, width, height)
    return None


def _pick_endpoint_colors(pixels, opaque_mode: bool) -> tuple[int, int]:
    """Picks the two BC1 endpoint colors as the pixel pair with the
    greatest Euclidean RGB distance (brute force over <=16 points - cheap).
    Using the actual furthest-apart pixels (instead of each channel's
    independent min/max, which can Frankenstein together channel extremes
    from unrelated pixels) matters a lot for blocks with hard color
    boundaries, e.g. an icon's opaque edge next to fully transparent
    background texels."""
    # Exclude fully-transparent texels from the fit regardless of
    # opaque_mode: even when the color block is encoded in forced 4-color
    # mode (BC2/BC3, where alpha lives in a separate block), an invisible
    # texel's color is noise that shouldn't skew the endpoints chosen for
    # the texels that actually show.
    candidates = [p[:3] for p in pixels if len(p) < 4 or p[3] > 0] or [p[:3] for p in pixels]
    if len(candidates) == 1:
        c = candidates[0]
        return _quantize_565(*c), _quantize_565(*c)
    best_pair, best_dist = (candidates[0], candidates[1]), -1
    for i in range(len(candidates)):
        for j in range(i + 1, len(candidates)):
            dist = sum((candidates[i][k] - candidates[j][k]) ** 2 for k in range(3))
            if dist > best_dist:
                best_dist, best_pair = dist, (candidates[i], candidates[j])
    return _quantize_565(*best_pair[0]), _quantize_565(*best_pair[1])


def _encode_bc1_color_block(pixels, opaque_mode: bool) -> bytes:
    """Simple 'range fit' BC1 encoder: picks the two endpoint colors from
    the block's most distant pixel pair. Not as accurate as a full
    least-squares/PCA encoder, but fast, dependency-free, and good enough
    for re-importing edited textures - lossy the same way any DXT re-encode
    is lossy."""
    lo, hi = _pick_endpoint_colors(pixels, opaque_mode)
    if lo > hi:
        lo, hi = hi, lo
    if opaque_mode:
        if hi <= lo:
            hi, lo = lo, lo + 1 if lo < 0xFFFF else lo - 1
        color0, color1 = hi, lo
    else:
        # Punch-through (BC1 with transparency): needs color0 <= color1.
        if lo > hi:
            lo, hi = hi, lo
        color0, color1 = lo, hi
    palette = _bc1_palette(color0, color1, punch_through=not opaque_mode)
    indices = 0
    for i, p in enumerate(pixels):
        if not opaque_mode and len(p) > 3 and p[3] < 128:
            idx = 3  # transparent slot
        else:
            best_idx, best_dist = 0, None
            for pi, pc in enumerate(palette):
                if not opaque_mode and pi == 3:
                    continue  # transparent slot only chosen for transparent pixels
                dist = sum((p[c] - pc[c]) ** 2 for c in range(3))
                if best_dist is None or dist < best_dist:
                    best_dist, best_idx = dist, pi
            idx = best_idx
        indices |= idx << (2 * i)
    return struct.pack("<HHI", color0, color1, indices)


def encode_bc1(rgba: bytes, width: int, height: int) -> bytes:
    out = bytearray()
    for bx, by, blocks_x in _iter_blocks(width, height):
        pixels = _gather_block(rgba, bx, by, width, height)
        opaque = all(p[3] >= 128 for p in pixels)
        out += _encode_bc1_color_block(pixels, opaque_mode=opaque)
    return bytes(out)


def encode_bc2(rgba: bytes, width: int, height: int) -> bytes:
    out = bytearray()
    for bx, by, blocks_x in _iter_blocks(width, height):
        pixels = _gather_block(rgba, bx, by, width, height)
        alpha_words = []
        for row in range(4):
            word = 0
            for col in range(4):
                a4 = min(15, (pixels[row * 4 + col][3] * 15 + 127) // 255)
                word |= a4 << (4 * col)
            alpha_words.append(word)
        out += struct.pack("<4H", *alpha_words)
        out += _encode_bc1_color_block(pixels, opaque_mode=True)
    return bytes(out)


def encode_bc3(rgba: bytes, width: int, height: int) -> bytes:
    out = bytearray()
    for bx, by, blocks_x in _iter_blocks(width, height):
        pixels = _gather_block(rgba, bx, by, width, height)
        alphas = [p[3] for p in pixels]
        a0, a1 = max(alphas), min(alphas)
        if a0 == a1:
            a1 = max(0, a0 - 1)  # force 8-step mode so every index maps to a0
        lut = [a0, a1] + [((7 - i) * a0 + i * a1) // 7 for i in range(1, 7)]
        abits = 0
        for i, a in enumerate(alphas):
            best_idx = min(range(8), key=lambda k: abs(lut[k] - a))
            abits |= best_idx << (3 * i)
        out += bytes((a0, a1)) + abits.to_bytes(6, "little")
        out += _encode_bc1_color_block(pixels, opaque_mode=True)
    return bytes(out)


def _gather_block(rgba: bytes, bx: int, by: int, width: int, height: int) -> list[tuple[int, int, int, int]]:
    pixels = []
    for ty in range(4):
        py = min(by * 4 + ty, height - 1)
        for tx in range(4):
            px = min(bx * 4 + tx, width - 1)
            o = (py * width + px) * 4
            pixels.append(tuple(rgba[o:o + 4]))
    return pixels


def encode_rgba_to_dxt(rgba: bytes, width: int, height: int, fourcc: str = "DXT5") -> bytes:
    if fourcc == "DXT1":
        return encode_bc1(rgba, width, height)
    if fourcc == "DXT3":
        return encode_bc2(rgba, width, height)
    if fourcc == "DXT5":
        return encode_bc3(rgba, width, height)
    raise ValueError(f"Unsupported encode fourcc: {fourcc!r} (use DXT1, DXT3 or DXT5)")


# --- tex_ chunk <-> structured data ----------------------------------------


def _find_child(body: "Body", tag: str) -> Optional["Chunk"]:
    for ch in body.chunks:
        if ch.tag == tag:
            return ch
    return None


def decode_texture_chunk(payload: bytes) -> dict:
    """Parses a tex_ chunk payload into name/formats/faces/mip levels
    without decoding pixels (that's decode_texture_chunk_to_rgba's job) -
    kept separate so callers that just want metadata (or an unsupported
    fourcc) don't pay for a decode that might not even be possible."""
    body = parse_body(payload)
    name_chunk = _find_child(body, "NAME")
    name = decode_ascii(name_chunk.payload) if name_chunk else ""
    formats = []
    for fmt_chunk in body.chunks:
        if fmt_chunk.tag != "FMT_":
            continue
        fmt_body = parse_body(fmt_chunk.payload)
        info_chunk = _find_child(fmt_body, "INFO")
        if info_chunk is None or len(info_chunk.payload) < 16:
            continue
        fourcc_b, width, height, depth, mip_count, kind, reserved = struct.unpack_from(
            "<4sHHHHHH", info_chunk.payload, 0)
        fourcc = fourcc_b.decode("ascii", "replace")
        faces = []
        for face_chunk in fmt_body.chunks:
            if face_chunk.tag != "FACE":
                continue
            face_body = parse_body(face_chunk.payload)
            levels = []
            for lvl_chunk in face_body.chunks:
                if lvl_chunk.tag != "LVL_":
                    continue
                lvl_body = parse_body(lvl_chunk.payload)
                lvl_info = _find_child(lvl_body, "INFO")
                lvl_data = _find_child(lvl_body, "BODY")
                mip_index, byte_size = struct.unpack("<II", lvl_info.payload) if lvl_info else (len(levels), 0)
                levels.append({"mip_index": mip_index, "data": lvl_data.payload if lvl_data else b""})
            faces.append(levels)
        formats.append({
            "fourcc": fourcc, "width": width, "height": height, "depth": depth,
            "mip_count": mip_count, "kind": kind, "reserved": reserved, "faces": faces,
        })
    return {"name": name, "formats": formats}


def decode_texture_chunk_to_rgba(payload: bytes) -> Optional[dict]:
    """Decodes the primary format/face/top mip level of a tex_ chunk to
    RGBA8888. Returns None if the stored fourcc isn't a format this module
    knows how to decode (caller should fall back to a raw/.dds export)."""
    info = decode_texture_chunk(payload)
    if not info["formats"]:
        return None
    fmt = info["formats"][0]
    if not fmt["faces"] or not fmt["faces"][0]:
        return None
    top_level = fmt["faces"][0][0]
    rgba = decode_dxt_to_rgba(fmt["fourcc"], top_level["data"], fmt["width"], fmt["height"])
    if rgba is None:
        return None
    return {"name": info["name"], "width": fmt["width"], "height": fmt["height"],
            "fourcc": fmt["fourcc"], "rgba": rgba}


# Stock SWBF1 textures (surveyed across every .lvl in this repo) come in
# format pairs - a DXT one plus an uncompressed D3DFMT fallback - each with a
# full mip chain: opaque = DXT1 + R5G6B5 (0x17), alpha = DXT3 + A4R4G4B4
# (0x1A). DXT5 never appears in the game's own data.
D3DFMT_R5G6B5 = 0x17
D3DFMT_A4R4G4B4 = 0x1A


def _to_r5g6b5(rgba: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(rgba), 4):
        r, g, b = rgba[i], rgba[i + 1], rgba[i + 2]
        out += struct.pack("<H", ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3))
    return bytes(out)


def _to_a4r4g4b4(rgba: bytes) -> bytes:
    out = bytearray()
    for i in range(0, len(rgba), 4):
        r, g, b, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
        out += struct.pack("<H", ((a >> 4) << 12) | ((r >> 4) << 8) | ((g >> 4) << 4) | (b >> 4))
    return bytes(out)


def _mip_chain(width: int, height: int, rgba: bytes) -> list:
    from PIL import Image
    img = Image.frombytes("RGBA", (width, height), rgba)
    levels = [(width, height, rgba)]
    w, h = width, height
    while w > 1 or h > 1:
        w, h = max(1, w // 2), max(1, h // 2)
        levels.append((w, h, img.resize((w, h), Image.LANCZOS).tobytes()))
    return levels


def encode_texture_chunk(name: str, width: int, height: int, rgba: bytes) -> bytes:
    """Builds a tex_ payload from an RGBA8888 image in the same layout the
    game's own textures use: DXT1 + R5G6B5 (opaque) or DXT3 + A4R4G4B4
    (any alpha), both with a full mip chain down to 1x1."""
    opaque = all(rgba[i] == 255 for i in range(3, len(rgba), 4))
    levels = _mip_chain(width, height, rgba)
    dxt = "DXT1" if opaque else "DXT3"
    raw = _to_r5g6b5 if opaque else _to_a4r4g4b4
    formats = [
        (dxt.encode("ascii"), [encode_rgba_to_dxt(px, w, h, dxt) for w, h, px in levels]),
        (struct.pack("<I", D3DFMT_R5G6B5 if opaque else D3DFMT_A4R4G4B4), [raw(px) for w, h, px in levels]),
    ]
    return _build_texture_payload_formats(name, width, height, formats)


def _build_texture_payload_formats(name: str, width: int, height: int, formats: list) -> bytes:
    """formats = [(4-byte fourcc/D3DFMT, [mip level bytes, largest first])]."""
    fmt_blocks = []
    for fourcc, levels in formats:
        lvls = b"".join(
            tagged_block(b"LVL_", tagged_block(b"INFO", struct.pack("<II", i, len(data))) + tagged_block(b"BODY", data))
            for i, data in enumerate(levels))
        info = tagged_block(b"INFO", struct.pack("<4sHHHHI", fourcc, width, height, 1, len(levels), 1))
        fmt_blocks.append(tagged_block(b"FMT_", info + tagged_block(b"FACE", lvls)))
    name_block = tagged_block(b"NAME", (sanitize_name(name) or "new").encode("ascii", "replace") + b"\x00")
    top_info = tagged_block(b"INFO", struct.pack("<I", len(formats)) + b"".join(f for f, _ in formats))
    return name_block + top_info + b"".join(fmt_blocks)


# --- Standard .dds <-> tex_ chunk (lossless container conversion) ----------
# Unlike PNG (which needs real pixel decode/encode above), DDS just wraps
# the tex_ chunk's BODY bytes as-is - verified against swbf-unmunge.exe's
# own '-imgfmt dds' output, so this direction never loses precision.

_DDS_MAGIC = b"DDS "
_DDS_HEADER_LEN = 124
_DDPF_FOURCC = 0x4
# CAPS|HEIGHT|WIDTH|PIXELFORMAT|MIPMAPCOUNT|LINEARSIZE - matches the flags
# swbf-unmunge.exe's own '-imgfmt dds' output uses (0xa1007), confirmed by
# hexdumping its output in this repo.
_DDSD_FLAGS = 0x1 | 0x2 | 0x4 | 0x1000 | 0x20000 | 0x80000
_DDSCAPS_TEXTURE = 0x1000


def _dxt_block_size(fourcc: str) -> int:
    return 8 if fourcc == "DXT1" else 16


def texture_chunk_to_dds(payload: bytes) -> Optional[bytes]:
    """Wraps a tex_ chunk's primary format/face/top mip as a standard .dds
    file. Works for any fourcc (not just the ones this module can decode to
    RGBA), since it just repackages the already-compressed bytes. Built
    piecewise (not one struct.pack format string) so the int/bytes fields
    can't silently misalign."""
    info = decode_texture_chunk(payload)
    if not info["formats"]:
        return None
    fmt = info["formats"][0]
    if not fmt["faces"] or not fmt["faces"][0]:
        return None
    top_level = fmt["faces"][0][0]
    width, height = fmt["width"], fmt["height"]
    linear_size = len(top_level["data"])
    header = b"".join([
        struct.pack("<7I", _DDS_HEADER_LEN, _DDSD_FLAGS, height, width, linear_size, 1, max(1, fmt["mip_count"])),
        b"\x00" * 44,  # dwReserved1[11]
        struct.pack("<2I4sI4I", 32, _DDPF_FOURCC, fmt["fourcc"].encode("ascii"), 0, 0, 0, 0, 0),
        struct.pack("<5I", _DDSCAPS_TEXTURE, 0, 0, 0, 0),
    ])
    assert len(header) == _DDS_HEADER_LEN
    return _DDS_MAGIC + header + top_level["data"]


def dds_to_texture_chunk(name: str, dds_bytes: bytes) -> bytes:
    """Inverse of texture_chunk_to_dds: repackages a standard .dds file's
    pixel data as a new tex_ chunk payload, preserving the fourcc/format so
    already-DXT-compressed .dds files round-trip losslessly."""
    if dds_bytes[:4] != _DDS_MAGIC or len(dds_bytes) < 4 + _DDS_HEADER_LEN:
        raise ValueError("Not a DDS file (missing 'DDS ' magic / header too short).")
    hdr = dds_bytes[4:4 + _DDS_HEADER_LEN]
    _size, _flags, height, width, _linear_size, _depth, mip_count = struct.unpack_from("<7I", hdr, 0)
    fourcc = hdr[80:84].decode("ascii", "replace")  # ddspf.dwFourCC (pixelformat starts at hdr offset 72)
    pixel_data = dds_bytes[4 + _DDS_HEADER_LEN:]
    compressed_len = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * _dxt_block_size(fourcc)
    compressed = pixel_data[:compressed_len]
    return _build_texture_payload(name, width, height, compressed, fourcc)


def _build_texture_payload(name: str, width: int, height: int, compressed: bytes, fourcc: str) -> bytes:
    body_block = tagged_block(b"BODY", compressed)
    lvl_info = tagged_block(b"INFO", struct.pack("<II", 0, len(compressed)))
    lvl_block = tagged_block(b"LVL_", lvl_info + body_block)
    face_block = tagged_block(b"FACE", lvl_block)
    fmt_info = tagged_block(b"INFO", struct.pack("<4sHHHHHH", fourcc.encode("ascii"), width, height, 1, 1, 1, 0))
    fmt_block = tagged_block(b"FMT_", fmt_info + face_block)
    name_block = tagged_block(b"NAME", (sanitize_name(name) or "new").encode("ascii", "replace") + b"\x00")
    top_info = tagged_block(b"INFO", struct.pack("<I4s", 1, fourcc.encode("ascii")))
    return name_block + top_info + fmt_block


# --- Generic "chunk -> usable file" / "usable file -> chunk" dispatcher ----
# Every chunk can already be exported/imported as raw framed bytes (see the
# IDE's "Export Chunk Bytes" / "Replace Payload From File"); these two
# functions add real format conversion on top of that for tags we understand
# (currently just tex_ <-> .png/.dds), and fall back to the raw bytes for
# every other tag so "any chunk type" is always exportable/importable.


def export_chunk_to_file(chunk: "Chunk", path) -> "Path":
    """Writes the most 'usable' representation of `chunk` to `path`.
    `path`'s extension may be swapped (e.g. tex_ always becomes .png/.dds
    regardless of what was passed in) so the file opens in a normal image
    viewer/editor. Returns the actual path written."""
    from pathlib import Path as _Path
    path = _Path(path)
    if chunk.tag == "tex_":
        if path.suffix.lower() == ".dds":
            dds = texture_chunk_to_dds(chunk.payload)
            if dds is not None:
                path.write_bytes(dds)
                return path
        decoded = decode_texture_chunk_to_rgba(chunk.payload)
        if decoded is not None:
            try:
                from PIL import Image
            except ImportError:
                pass
            else:
                png_path = path.with_suffix(".png")
                Image.frombytes("RGBA", (decoded["width"], decoded["height"]), decoded["rgba"]).save(png_path)
                return png_path
        # Fourcc we can't decode to RGBA (or no Pillow) - fall back to .dds,
        # which never needs pixel decoding.
        dds = texture_chunk_to_dds(chunk.payload)
        if dds is not None:
            dds_path = path.with_suffix(".dds")
            dds_path.write_bytes(dds)
            return dds_path
    if chunk.tag == "scr_":
        script = decode_script_chunk(chunk.payload)
        try:
            if path.suffix.lower() == ".luac":
                raise ValueError("raw bytecode asked for")
            decompile = lua_decompile
            source = decompile(script["code"])
        except Exception:
            # every stock script decompiles, but a modded one might use a
            # pattern the decompiler doesn't know - keep the bytecode then
            luac_path = path.with_suffix(".luac")
            luac_path.write_bytes(script["code"])
            return luac_path
        lua_path = path.with_suffix(".lua")
        lua_path.write_text(source, encoding="utf-8")
        return lua_path
    if chunk.tag == "skel":
        import json
        json_path = path.with_suffix(".json")
        json_path.write_text(json.dumps(decode_skeleton_bone(chunk.payload), indent=2))
        return json_path
    if chunk.tag in KV_TAGS:
        odf_path = path.with_suffix(".odf")
        odf_path.write_text(encode_kv_chunk_to_odf_text(chunk.tag, chunk.payload), encoding="ascii", errors="replace")
        return odf_path
    if chunk.tag in CONFIG_TEXT_TAGS:
        text_path = path if path.suffix else path.with_suffix(CONFIG_TAGS[chunk.tag])
        text_path.write_text(config_decode_text(chunk.payload), encoding="utf-8")
        return text_path
    if chunk.tag == "zaa_":
        import json
        json_path = path.with_suffix(".json")
        json_path.write_text(json.dumps(bank_to_dict(decode_zaa(chunk.payload)), indent=1))
        return json_path
    if chunk.tag in STRUCTURED_BINARY_TAGS:
        import json
        json_path = path.with_suffix(".json")
        json_path.write_text(json.dumps(decode_structured_binary_tree(chunk.payload), indent=2))
        return json_path
    path.write_bytes(chunk.raw_chunk_bytes())
    return path


def import_file_to_chunk_payload(tag: str, path) -> bytes:
    """Inverse of export_chunk_to_file: given a chunk tag and a file (the
    specialized 'usable' format for that tag if one exists, e.g. a .png/.dds
    for tex_; otherwise a raw exported chunk or bare payload), returns the
    bytes to use as that chunk's new payload."""
    from pathlib import Path as _Path
    path = _Path(path)
    suffix = path.suffix.lower()
    if tag == "tex_" and suffix == ".dds":
        return dds_to_texture_chunk(path.stem, path.read_bytes())
    if tag == "tex_" and suffix in (".png", ".bmp", ".tga", ".jpg", ".jpeg"):
        from PIL import Image  # raises ImportError with a clear message if missing
        img = Image.open(path).convert("RGBA")
        return encode_texture_chunk(path.stem, img.width, img.height, img.tobytes())
    if tag == "scr_" and suffix == ".lua":
        # Lua source: compiled to the game's Lua 4.0 bytecode by bf1_lua_compile
        try:
            code = lua_compile(path.read_text(encoding="utf-8-sig"))
        except LuaSyntaxError as exc:
            raise ValueError(f"{path.name}, {exc}") from None
        return encode_script_chunk(path.stem, code)
    if tag == "scr_" and suffix in (".luac", ".bin"):
        return encode_script_chunk(path.stem, path.read_bytes())
    if tag in CONFIG_TEXT_TAGS and suffix not in (".bin", ".raw"):
        return _config_payload(tag, path)
    if tag == "zaa_" and suffix == ".json":
        import json
        return encode_zaa(bank_from_dict(json.loads(path.read_text(encoding="utf-8"))))
    if tag in KV_TAGS and suffix == ".odf":
        return decode_odf_text_to_kv_payload(path.read_text(encoding="ascii", errors="replace"),
                                             fallback_type=path.stem)
    data = path.read_bytes()
    if data[:4] == tag.encode("ascii", "replace") and len(data) >= 8:
        length = struct.unpack_from("<I", data, 4)[0]
        return data[8:8 + length]
    return data


CONFIG_TEXT_TAGS = {"fx__", "sky_", "bnd_", "prp_", "load", "snd_", "mcfg"}


def import_file_into_chunk(chunk: "Chunk", path) -> bytes:
    """The new payload for `chunk` from a file (import_file_to_chunk_payload), keeping
    what identifies the chunk rather than taking it from the file name: a script keeps
    its name (the game loads scripts by name) and a config keeps its NAME hash."""
    from pathlib import Path as _Path
    path = _Path(path)
    payload = import_file_to_chunk_payload(chunk.tag, path)
    if chunk.tag == "scr_":
        old, new = decode_script_chunk(chunk.payload), decode_script_chunk(payload)
        return encode_script_chunk(old["name"] or new["name"], new["code"])
    if chunk.tag in CONFIG_TEXT_TAGS and path.suffix.lower() not in (".bin", ".raw"):
        return config_encode_text(chunk.payload, path.read_text(encoding="utf-8-sig"))
    return payload


def _config_payload(tag, path) -> bytes:
    """A config chunk payload from its text form (see bf1_config). The NAME hash
    is the file name's, as the munger writes it."""
    try:
        lines = config_from_text(path.read_text(encoding="utf-8-sig"))
    except ConfigError as exc:
        raise ValueError(f"{path.name}: {exc}") from None
    return config_encode(fnv1a32(path.stem), lines)


# --- Script ("scr_") chunk codec -------------------------------------------
# Layout (found the same way as tex_ - exploding a scr_ chunk with
# swbf-unmunge.exe -mode explode):
#   NAME - null-terminated script name
#   INFO - single byte, always seen as 0x01 in this repo's sample data
#          (plausibly a format/version tag - not a length or count, there's
#          nothing else it could index)
#   BODY - the compiled script: Lua 4.0 bytecode (0x1B "Lua" 0x40) built
#          with 4-byte floats and size_t. bf1_lua_decompile turns it into
#          Lua source and bf1_lua_compile compiles source back.

_LUA_BYTECODE_SIGNATURE = b"\x1bLua"


def decode_script_chunk(payload: bytes) -> dict:
    body = parse_body(payload)
    name_chunk = _find_child(body, "NAME")
    body_chunk = _find_child(body, "BODY")
    return {
        "name": decode_ascii(name_chunk.payload) if name_chunk else "",
        "code": body_chunk.payload if body_chunk else b"",
        "is_lua_bytecode": bool(body_chunk) and body_chunk.payload[:4] == _LUA_BYTECODE_SIGNATURE,
    }


def encode_script_chunk(name: str, code: bytes) -> bytes:
    """A scr_ payload. Every stock BODY is the Lua chunk plus one zero byte, and the
    game reads one byte less than BODY's size - without it, the last byte of the
    script is cut off and the script fails to load."""
    if code[:4] == _LUA_BYTECODE_SIGNATURE:
        try:
            chunk_length = lua_chunk_length
            if chunk_length(code) == len(code):
                code = code + b"\x00"
        except Exception:
            pass
    name_block = tagged_block(b"NAME", (sanitize_name(name) or "new").encode("ascii", "replace") + b"\x00")
    info_block = tagged_block(b"INFO", b"\x01")
    body_block = tagged_block(b"BODY", code)
    return name_block + info_block + body_block


# --- Skeleton ("skel") chunk codec ------------------------------------------
# Each top-level 'skel' chunk is exactly ONE bone, not a whole skeleton -
# found by exploding one with swbf-unmunge.exe -mode explode:
#   INFO - null-terminated owner model name, u32 total_bone_count (the
#          model this bone belongs to, and how many 'skel' chunks together
#          make up its full skeleton - confirmed by cross-checking every
#          skel chunk in this repo's modded_lvl/shell.lvl: owner names line
#          up with real 'modl'/'gmod' chunk names in the same file)
#   NAME - this bone's own name
#   PRNT - single byte, parent bone index within the skeleton (0 in every
#          sample here, all of which are trivial single-bone rigs for
#          static menu icons - the multi-bone indexing scheme itself is
#          unverified since no multi-bone sample was available to check)
#   XFRM - 12 floats as 4 rows of 3: the first 3 rows are the rotation/scale
#          basis vectors (x/y/z axes), the 4th row is the translation -
#          confirmed against every sample in this repo's modded_lvl/
#          shell.lvl, all identity-rotation/zero-translation: rows
#          [1,0,0], [0,1,0], [0,0,1], [0,0,0].


def decode_skeleton_bone(payload: bytes) -> dict:
    body = parse_body(payload)
    info_chunk = _find_child(body, "INFO")
    name_chunk = _find_child(body, "NAME")
    prnt_chunk = _find_child(body, "PRNT")
    xfrm_chunk = _find_child(body, "XFRM")
    owner_model, bone_count = "", 0
    if info_chunk is not None:
        raw = info_chunk.payload
        nul = raw.find(b"\x00")
        if nul != -1 and len(raw) >= nul + 5:
            owner_model = decode_ascii(raw[:nul])
            bone_count = struct.unpack_from("<I", raw, nul + 1)[0]
    transform = None
    if xfrm_chunk is not None and len(xfrm_chunk.payload) >= 48:
        floats = struct.unpack_from("<12f", xfrm_chunk.payload, 0)
        transform = [list(floats[i * 3:i * 3 + 3]) for i in range(4)]
    return {
        "owner_model": owner_model,
        "bone_count": bone_count,
        "bone_name": decode_ascii(name_chunk.payload) if name_chunk else "",
        "parent_index": prnt_chunk.payload[0] if prnt_chunk and prnt_chunk.payload else 0,
        "transform": transform,
    }


# --- Model ("modl"/"gmod"/"coll") export via the bundled swbf-unmunge.exe --
# Textures (tex_) and ordnance (ordc/wpnc/expc) were tractable to fully
# reverse-engineer and reimplement above - the wire format is a couple
# dozen bytes of header per block. A complete skinned/LOD'd mesh format
# (vertex buffers in several possible layouts, index buffers, material/
# shader bindings, per-LOD segments, collision hulls, and the bone-weight
# links into 'skel') is a whole different scale of effort, and this
# repo already ships a mature, validated tool that does exactly that:
# swbf-unmunge.exe. So model export here works by re-using it directly,
# rather than re-deriving the mesh format from scratch:
#   1. A single 'modl' chunk is metadata (materials, LOD/segment list, the
#      name of the 'skel' bones it uses) and is useless without its
#      matching 'gmod' chunk (the actual geometry), which always shares the
#      same TYPE name - confirmed across all 24 modl/gmod pairs in this
#      repo's modded_lvl/shell.lvl.
#   2. Its skeleton (if any) is one-or-more top-level 'skel' chunks whose
#      own INFO block names this model as their owner (see
#      decode_skeleton_bone above) - not nested inside modl/gmod at all.
#   3. Bundling exactly those chunks (modl + gmod + matching coll + matching
#      skel bones) into a synthetic single-chunk ucfb file and running
#      `swbf-unmunge.exe -mode extract -modelfmt glTF` on it reproduces
#      byte-for-byte the same .glb that unmunging the entire original .lvl
#      produces - verified in this repo against modded_lvl/shell.lvl's
#      "planet_bes" model (cmp came back identical).
# NOTE: this covers chunk -> .glb (export) for modl/gmod/coll all alike.
# The reverse (.glb -> chunk) is NOT symmetric: swbf-unmunge has no
# 'assemble' support for glTF (only for its own exploded chunk trees), and
# gmod/coll/skel's own formats are only understood well enough here to
# unmunge them, not confidently rebuild them from scratch. modl is the one
# exception - see bf1_glb_import.py, which reverse-engineered the VBUF/IBUF/
# segm layout against swbf-unmunge's actual source (not guesswork) well
# enough to replace an existing modl chunk's geometry from a .glb while
# preserving its materials/textures. gmod/coll/skel remain export-only.

MODEL_TAGS = {"modl", "gmod", "coll"}


def app_dir() -> Path:
    """Where settings live: next to the .exe in a release build, next to the
    scripts otherwise (a one-file .exe unpacks itself to a temp folder, so
    __file__ isn't somewhere that survives a restart)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_path(name: str) -> Path:
    """A file shipped with the tools (swbf-unmunge.exe, icon.ico): inside
    the one-file .exe's bundle, else next to the .exe/scripts."""
    bundled = Path(getattr(sys, "_MEIPASS", "")) / name
    if getattr(sys, "_MEIPASS", None) and bundled.exists():
        return bundled
    return app_dir() / name


def _find_swbf_unmunge() -> Optional[Path]:
    exe_name = "swbf-unmunge.exe" if sys.platform == "win32" else "swbf-unmunge"
    candidate = resource_path(exe_name)
    return candidate if candidate.exists() else None


def gather_model_bundle(chunks: list["Chunk"], target: "Chunk") -> list["Chunk"]:
    """Given the full chunk list of the container `target` lives in, finds
    every chunk needed to unmunge `target` (a modl/gmod/coll chunk) into a
    complete model: its name-matching modl/gmod/coll siblings, plus every
    'skel' bone chunk whose INFO names this model as owner."""
    if target.tag not in MODEL_TAGS:
        raise ValueError(f"Not a model chunk tag: {target.tag!r} (expected one of {sorted(MODEL_TAGS)})")
    model_name = _model_chunk_name(target)
    bundle = [ch for ch in chunks if ch.tag in MODEL_TAGS and _model_chunk_name(ch) == model_name]
    bundle += [ch for ch in chunks if ch.tag == "skel" and decode_skeleton_bone(ch.payload)["owner_model"] == model_name]
    return bundle


def _model_chunk_name(chunk: "Chunk") -> str:
    """modl/gmod/coll all wrap a plain NAME block (unlike ordc/wpnc/expc),
    so display_name() already does the right thing."""
    return chunk.display_name()


def export_model_bundle_to_file(chunks: list["Chunk"], target: "Chunk", path, model_format: str = "glTF") -> "Path":
    """Exports `target` (a modl/gmod/coll chunk) plus whatever siblings it
    needs (see gather_model_bundle) to a real, viewable model file by
    shelling out to the bundled swbf-unmunge.exe. Returns the path actually
    written (`path`'s extension is replaced with .glb/.msh to match)."""
    from pathlib import Path as _Path
    path = _Path(path)
    exe = _find_swbf_unmunge()
    if exe is None:
        raise FileNotFoundError("swbf-unmunge.exe not found next to bf1_core.py - can't export model chunks.")
    bundle = gather_model_bundle(chunks, target)
    model_name = _model_chunk_name(target)
    with tempfile.TemporaryDirectory(prefix="bf1_model_export_") as tmp:
        tmp_dir = _Path(tmp)
        bundle_body = Body(b"", [Chunk(ch.tag, ch.payload) for ch in bundle])
        bundle_file = tmp_dir / "bundle.lvl"
        bundle_file.write_bytes(Container(True, bundle_body).raw_bytes())
        result = subprocess.run(
            [str(exe), "-file", str(bundle_file), "-mode", "extract", "-modelfmt", model_format],
            cwd=tmp_dir, capture_output=True, text=True, timeout=60,
        )
        out_dir = tmp_dir / "bundle" / "models"
        ext = ".glb" if model_format == "glTF" else ".msh"
        produced = out_dir / f"{model_name}{ext}"
        if not produced.exists():
            raise RuntimeError(
                f"swbf-unmunge didn't produce {produced.name} for model '{model_name}'.\n"
                f"stdout:\n{result.stdout[-2000:]}\nstderr:\n{result.stderr[-2000:]}")
        dest = path.with_suffix(ext)
        dest.write_bytes(produced.read_bytes())
        return dest


# --- "zaa_"/"zaf_" (animation/skin binary) structural viewer ---------------
# Both are a NAME block, then a 'BIN_' block whose payload is a tree of
# tag+length+payload blocks with each 4-byte tag stored BYTE-REVERSED ('BIN_'
# holds 'ZAFF', 'VERS', 'SKEL', ... read backwards). bf1_anim.py decodes them
# fully - zaa_ animation banks are edited there (the editor's animation panel
# and `bf1 anim`). This generic block-tree view is what's left for zaf_
# (skeleton, skin and material tables) and for anything bf1_anim can't read.

STRUCTURED_BINARY_TAGS = {"zaa_", "zaf_"}


def _looks_like_reversed_tag_header(data: bytes, offset: int) -> bool:
    if offset + 8 > len(data):
        return False
    tag = data[offset:offset + 4][::-1]
    if not all(32 <= b < 127 for b in tag):
        return False
    length = struct.unpack_from("<I", data, offset + 4)[0]
    return 0 <= length <= len(data) - offset - 8


def _parse_reversed_tag_tree(data: bytes, max_depth: int = 4) -> list[dict]:
    nodes = []
    pos = 0
    n = len(data)
    while pos + 8 <= n and _looks_like_reversed_tag_header(data, pos):
        tag = data[pos:pos + 4][::-1].decode("ascii")
        length = struct.unpack_from("<I", data, pos + 4)[0]
        payload = data[pos + 8:pos + 8 + length]
        node = {"tag": tag, "offset": pos, "length": length}
        if max_depth > 0 and length >= 8 and _looks_like_reversed_tag_header(payload, 0):
            children = _parse_reversed_tag_tree(payload, max_depth - 1)
            if children:
                node["children"] = children
        if "children" not in node:
            node["payload_preview"] = payload[:32].hex()
        nodes.append(node)
        pos += 8 + length
    if pos < n:
        nodes.append({"unparsed_tail_offset": pos, "unparsed_tail_length": n - pos,
                       "unparsed_tail_preview": data[pos:pos + 32].hex()})
    return nodes


def decode_structured_binary_tree(payload: bytes) -> dict:
    """Best-effort structural breakdown of a zaa_/zaf_ chunk payload - NOT a
    full decode (see the module comment above this function for why), just
    enough to browse what's actually in there instead of a flat hexdump."""
    body = parse_body(payload)
    name_chunk = _find_child(body, "NAME")
    bin_chunk = _find_child(body, "BIN_")
    return {
        "name": decode_ascii(name_chunk.payload) if name_chunk else "",
        "tree": _parse_reversed_tag_tree(bin_chunk.payload) if bin_chunk else [],
    }


# ==================================================================================================
# bf1_lua_compile - merged in from bf1_lua_compile.py
# ==================================================================================================
# bf1_lua_compile.py - Lua 4.0 source -> bytecode compiler, so edited scripts
# (scr_ chunks) can go back into the game. Pure Python, no luac needed.
#
# This game runs Lua 4.0 with its own build settings: 4-byte size_t and
# 4-byte float numbers (the stock luac 4.0 writes 8-byte doubles, which the
# game rejects). compile() writes that format by default.
#
# It's a port of Lua 4.0.1's own compiler - llex.c (the lexer), lparser.c
# (the grammar) and lcode.c (code generation, including its peephole
# optimizations: GETDOTTED, ADDI, TAILCALL, merged PUSHNILs, ...) - kept close
# to the C so the output matches luac 4.0.1 instruction for instruction (it's
# checked against luac on test sources: see tools/check_lua_compiler.py).
#
#   compile(source, name="=(none)", strip=True) -> bytes
#
# Differences from stock luac 4.0.1, all matching how the game's scripts were
# compiled: table constructors are flushed every 37 items (not 64), a number
# constant is reused from anywhere in the function (not just the last 20), and
# a call in parentheses - `f((g()))` - passes on just its first result.


# --- opcodes (lopcodes.h) -------------------------------------------------------------------

(OP_END, OP_RETURN, OP_CALL, OP_TAILCALL, OP_PUSHNIL, OP_POP, OP_PUSHINT, OP_PUSHSTRING, OP_PUSHNUM,
 OP_PUSHNEGNUM, OP_PUSHUPVALUE, OP_GETLOCAL, OP_GETGLOBAL, OP_GETTABLE, OP_GETDOTTED, OP_GETINDEXED,
 OP_PUSHSELF, OP_CREATETABLE, OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE, OP_SETLIST, OP_SETMAP, OP_ADD,
 OP_ADDI, OP_SUB, OP_MULT, OP_DIV, OP_POW, OP_CONCAT, OP_MINUS, OP_NOT, OP_JMPNE, OP_JMPEQ, OP_JMPLT,
 OP_JMPLE, OP_JMPGT, OP_JMPGE, OP_JMPT, OP_JMPF, OP_JMPONT, OP_JMPONF, OP_JMP, OP_PUSHNILJMP, OP_FORPREP,
 OP_FORLOOP, OP_LFORPREP, OP_LFORLOOP, OP_CLOSURE) = range(49)

SIZE_OP, SIZE_B = 6, 9
SIZE_U = 32 - SIZE_OP
POS_B = SIZE_OP
POS_A = SIZE_OP + SIZE_B
MAXARG_U = (1 << SIZE_U) - 1
MAXARG_S = MAXARG_U >> 1
MAXARG_A = (1 << (32 - POS_A)) - 1
MAXARG_B = (1 << SIZE_B) - 1
NO_JUMP = -1
MULT_RET = 255
MAXSTACK = 256
MAXLOCALS = 200
MAXUPVALUES = 32
MAXVARSLH = 100
MAXPARAMS = 100
LFIELDS_PER_FLUSH = 64   # stock Lua 4.0; the game's build flushes table constructors every 37
GAME_LFIELDS_PER_FLUSH = 37
LOOKBACKNUMS = 20   # stock Lua 4.0 reuses a number constant from the last 20; the game's build, any
MAX_INT = 2147483647 - 2

iO, iU, iS, iAB = range(4)
VD = 100  # "variable delta"
# (mode, push, pop) per opcode - luaK_opproperties
PROPS = [
    (iO, 0, 0), (iU, 0, 0), (iAB, 0, 0), (iAB, 0, 0), (iU, VD, 0), (iU, 0, VD), (iS, 1, 0), (iU, 1, 0),
    (iU, 1, 0), (iU, 1, 0), (iU, 1, 0), (iU, 1, 0), (iU, 1, 0), (iO, 1, 2), (iU, 1, 1), (iU, 1, 1),
    (iU, 2, 1), (iU, 1, 0), (iU, 0, 1), (iU, 0, 1), (iAB, 0, VD), (iAB, 0, VD), (iU, 0, VD), (iO, 1, 2),
    (iS, 1, 1), (iO, 1, 2), (iO, 1, 2), (iO, 1, 2), (iO, 1, 2), (iU, 1, VD), (iO, 1, 1), (iO, 1, 1),
    (iS, 0, 2), (iS, 0, 2), (iS, 0, 2), (iS, 0, 2), (iS, 0, 2), (iS, 0, 2), (iS, 0, 1), (iS, 0, 1),
    (iS, 0, 1), (iS, 0, 1), (iS, 0, 0), (iO, 0, 0), (iS, 0, 0), (iS, 0, 3), (iS, 2, 0), (iS, 0, 3),
    (iAB, 1, VD),
]


def ISJUMP(o):
    return OP_JMPNE <= o <= OP_JMP


def GET_OPCODE(i):
    return i & 0x3F


def GETARG_U(i):
    return (i >> POS_B) & MAXARG_U


def GETARG_S(i):
    return GETARG_U(i) - MAXARG_S


def GETARG_A(i):
    return (i >> POS_A) & MAXARG_A


def GETARG_B(i):
    return (i >> POS_B) & MAXARG_B


def CREATE_0(o):
    return o


def CREATE_U(o, u):
    return (o | (u << POS_B)) & 0xFFFFFFFF


def CREATE_S(o, s):
    return CREATE_U(o, s + MAXARG_S)


def CREATE_AB(o, a, b):
    return (o | (a << POS_A) | (b << POS_B)) & 0xFFFFFFFF


def SET_OPCODE(i, o):
    return (i & ~0x3F) | o


def SETARG_U(i, u):
    return (i & 0x3F) | ((u & MAXARG_U) << POS_B)


def SETARG_S(i, s):
    return SETARG_U(i, s + MAXARG_S)


def SETARG_B(i, b):
    return (i & ~(MAXARG_B << POS_B)) | ((b & MAXARG_B) << POS_B)


class LuaSyntaxError(Exception):
    def __init__(self, message: str, line: int):
        super().__init__(f"line {line}: {message}")
        self.line = line
        self.message = message


# --- lexer (llex.c) -------------------------------------------------------------------------

LUA_RESERVED_WORDS = ["and", "break", "do", "else", "elseif", "end", "for", "function", "if", "local", "nil",
            "not", "or", "repeat", "return", "then", "until", "while"]
TK_AND, TK_BREAK, TK_DO, TK_ELSE, TK_ELSEIF, TK_END, TK_FOR, TK_FUNCTION, TK_IF, TK_LOCAL, TK_NIL, \
    TK_NOT, TK_OR, TK_REPEAT, TK_RETURN, TK_THEN, TK_UNTIL, TK_WHILE = range(257, 257 + 18)
TK_NAME, TK_CONCAT, TK_DOTS, TK_EQ, TK_GE, TK_LE, TK_NE, TK_NUMBER, TK_STRING, TK_EOS = range(275, 285)
_RESERVED = {w: 257 + i for i, w in enumerate(LUA_RESERVED_WORDS)}
_TOKEN_TEXT = {**{v: k for k, v in _RESERVED.items()}, TK_NAME: "<name>", TK_CONCAT: "..", TK_DOTS: "...",
               TK_EQ: "==", TK_GE: ">=", TK_LE: "<=", TK_NE: "~=", TK_NUMBER: "<number>",
               TK_STRING: "<string>", TK_EOS: "<eof>"}


def token_text(t) -> str:
    return _TOKEN_TEXT.get(t) or (chr(t) if isinstance(t, int) else str(t))


class Lexer:
    def __init__(self, source: bytes):
        self.src = source
        self.pos = 0
        self.linenumber = 1
        if source[:1] == b"#":  # skip a first line like "#!/usr/bin/lua"
            while self.pos < len(source) and source[self.pos] != 10:
                self.pos += 1

    def cur(self) -> int:
        return self.src[self.pos] if self.pos < len(self.src) else -1

    def error(self, msg: str):
        raise LuaSyntaxError(msg, self.linenumber)

    def lex(self):
        """(token, seminfo)"""
        src = self.src
        while True:
            c = self.cur()
            if c == 10:
                self.linenumber += 1
                self.pos += 1
            elif c in (32, 9, 13, 11, 12):
                self.pos += 1
            elif c == -1:
                return TK_EOS, None
            elif c == ord("-"):
                self.pos += 1
                if self.cur() != ord("-"):
                    return ord("-"), None
                while self.cur() not in (10, -1):
                    self.pos += 1
            elif c == ord("["):
                self.pos += 1
                if self.cur() != ord("["):
                    return ord("["), None
                return TK_STRING, self.long_string()
            elif c == ord("="):
                self.pos += 1
                if self.cur() != ord("="):
                    return ord("="), None
                self.pos += 1
                return TK_EQ, None
            elif c == ord("<"):
                self.pos += 1
                if self.cur() != ord("="):
                    return ord("<"), None
                self.pos += 1
                return TK_LE, None
            elif c == ord(">"):
                self.pos += 1
                if self.cur() != ord("="):
                    return ord(">"), None
                self.pos += 1
                return TK_GE, None
            elif c == ord("~"):
                self.pos += 1
                if self.cur() != ord("="):
                    return ord("~"), None
                self.pos += 1
                return TK_NE, None
            elif c in (ord('"'), ord("'")):
                return TK_STRING, self.string(c)
            elif c == ord("."):
                self.pos += 1
                if self.cur() == ord("."):
                    self.pos += 1
                    if self.cur() == ord("."):
                        self.pos += 1
                        return TK_DOTS, None
                    return TK_CONCAT, None
                if not (48 <= self.cur() <= 57):
                    return ord("."), None
                self.pos -= 1
                return TK_NUMBER, self.number()
            elif 48 <= c <= 57:
                return TK_NUMBER, self.number()
            elif c == ord("_") or chr(c).isalpha():
                start = self.pos
                while self.cur() != -1 and (self.cur() == ord("_") or chr(self.cur()).isalnum()):
                    self.pos += 1
                word = src[start:self.pos]
                tok = _RESERVED.get(word.decode("latin-1"))
                return (tok, None) if tok else (TK_NAME, word)
            else:
                self.pos += 1
                return c, None

    def number(self) -> float:
        start = self.pos
        while 48 <= self.cur() <= 57:
            self.pos += 1
        if self.cur() == ord("."):
            self.pos += 1
            if self.cur() == ord("."):
                self.error("ambiguous syntax (decimal point x string concatenation)")
            while 48 <= self.cur() <= 57:
                self.pos += 1
        if self.cur() in (ord("e"), ord("E")):
            self.pos += 1
            if self.cur() in (ord("+"), ord("-")):
                self.pos += 1
            while 48 <= self.cur() <= 57:
                self.pos += 1
        text = self.src[start:self.pos].decode("latin-1")
        try:
            return float(text)
        except ValueError:
            self.error(f"malformed number near `{text}'")

    def long_string(self) -> bytes:
        self.pos += 1  # the second '['
        if self.cur() == 10:  # a first newline is skipped
            self.linenumber += 1
            self.pos += 1
        out = bytearray()
        level = 0
        while True:
            c = self.cur()
            if c == -1:
                self.error("unfinished long string")
            if c == ord("[") and self.src[self.pos + 1:self.pos + 2] == b"[":
                level += 1
                out += b"[["
                self.pos += 2
            elif c == ord("]") and self.src[self.pos + 1:self.pos + 2] == b"]":
                if level == 0:
                    self.pos += 2
                    return bytes(out)
                level -= 1
                out += b"]]"
                self.pos += 2
            else:
                if c == 10:
                    self.linenumber += 1
                out.append(c)
                self.pos += 1

    def string(self, delim: int) -> bytes:
        self.pos += 1
        out = bytearray()
        while True:
            c = self.cur()
            if c == delim:
                self.pos += 1
                return bytes(out)
            if c in (-1, 10):
                self.error("unfinished string")
            if c == ord("\\"):
                self.pos += 1
                c = self.cur()
                simple = {ord("a"): 7, ord("b"): 8, ord("f"): 12, ord("n"): 10, ord("r"): 13, ord("t"): 9,
                          ord("v"): 11}
                if c in simple:
                    out.append(simple[c])
                    self.pos += 1
                elif c == 10:
                    self.linenumber += 1
                    out.append(10)
                    self.pos += 1
                elif 48 <= c <= 57:
                    n = 0
                    for _ in range(3):
                        if not (48 <= self.cur() <= 57):
                            break
                        n = n * 10 + self.cur() - 48
                        self.pos += 1
                    if n > 255:
                        self.error("escape sequence too large")
                    out.append(n)
                elif c == -1:
                    self.error("unfinished string")
                else:
                    out.append(c)
                    self.pos += 1
            else:
                out.append(c)
                self.pos += 1


# --- compiler state -------------------------------------------------------------------------

VGLOBAL, VLOCAL, VINDEXED, VEXP = range(4)


class ExpDesc:
    __slots__ = ("k", "index", "t", "f")

    def __init__(self, k=VEXP, index=0):
        self.k, self.index, self.t, self.f = k, index, NO_JUMP, NO_JUMP

    def copy(self):
        e = ExpDesc(self.k, self.index)
        e.t, e.f = self.t, self.f
        return e


class Proto:
    def __init__(self, source: bytes):
        self.source = source
        self.line_defined = 0
        self.numparams = 0
        self.is_vararg = 0
        self.maxstacksize = 0
        self.code = []
        self.kstr = []
        self.kstr_index = {}
        self.knum = []
        self.kproto = []
        self.locvars = []   # [name, startpc, endpc]
        self.lineinfo = []


class Breaklabel:
    def __init__(self, stacklevel, previous):
        self.stacklevel = stacklevel
        self.breaklist = NO_JUMP
        self.previous = previous


class FuncState:
    def __init__(self, prev, source):
        self.f = Proto(source)
        self.prev = prev
        self.pc = 0
        self.lasttarget = 0
        self.jlt = NO_JUMP
        self.stacklevel = 0
        self.nactloc = 0
        self.nupvalues = 0
        self.lastline = 0
        self.bl = None
        self.upvalues = []
        self.actloc = []


UNARY_PRIORITY = 7
OPR_ADD, OPR_SUB, OPR_MULT, OPR_DIV, OPR_POW, OPR_CONCAT, OPR_NE, OPR_EQ, OPR_LT, OPR_LE, OPR_GT, OPR_GE, \
    OPR_AND, OPR_OR, OPR_NOBINOPR = range(15)
OPR_MINUS, OPR_NOT, OPR_NOUNOPR = range(3)
PRIORITY = [(5, 5), (5, 5), (6, 6), (6, 6), (9, 8), (4, 3), (2, 2), (2, 2), (2, 2), (2, 2), (2, 2), (2, 2),
            (1, 1), (1, 1)]
CODES = [(OP_ADD, 0), (OP_SUB, 0), (OP_MULT, 0), (OP_DIV, 0), (OP_POW, 0), (OP_CONCAT, 2),
         (OP_JMPNE, NO_JUMP), (OP_JMPEQ, NO_JUMP), (OP_JMPLT, NO_JUMP), (OP_JMPLE, NO_JUMP),
         (OP_JMPGT, NO_JUMP), (OP_JMPGE, NO_JUMP)]
_BINOPR = {ord("+"): OPR_ADD, ord("-"): OPR_SUB, ord("*"): OPR_MULT, ord("/"): OPR_DIV, ord("^"): OPR_POW,
           TK_CONCAT: OPR_CONCAT, TK_NE: OPR_NE, TK_EQ: OPR_EQ, ord("<"): OPR_LT, TK_LE: OPR_LE,
           ord(">"): OPR_GT, TK_GE: OPR_GE, TK_AND: OPR_AND, TK_OR: OPR_OR}


class Compiler:
    def __init__(self, source: bytes, chunkname: bytes, number_size: int, lfields: int = LFIELDS_PER_FLUSH):
        self.lfields = lfields
        self.rfields = lfields // 2
        self.lookback = LOOKBACKNUMS if lfields == LFIELDS_PER_FLUSH else 1 << 30
        self.lx = Lexer(source)
        self.source_name = chunkname
        self.number_size = number_size
        self.fs = None
        self.t = None          # current token
        self.seminfo = None
        self.ahead = None      # lookahead (token, seminfo) or None
        self.linenumber = 1
        self.lastline = 1

    # -- tokens

    def next(self):
        self.lastline = self.lx.linenumber
        if self.ahead is not None:
            (self.t, self.seminfo), self.ahead = self.ahead, None
        else:
            self.t, self.seminfo = self.lx.lex()

    def lookahead(self):
        self.ahead = self.lx.lex()

    def error(self, msg: str):
        raise LuaSyntaxError(msg, self.lx.linenumber)

    def error_expected(self, token):
        self.error(f"`{token_text(token)}' expected")

    def check(self, c):
        if self.t != c:
            self.error_expected(c)
        self.next()

    def check_condition(self, cond, msg):
        if not cond:
            self.error(msg)

    def optional(self, c) -> bool:
        if self.t == c:
            self.next()
            return True
        return False

    def check_match(self, what, who, where):
        if self.t != what:
            if where == self.lx.linenumber:
                self.error_expected(what)
            self.error(f"`{token_text(what)}' expected (to close `{token_text(who)}' at line {where})")
        self.next()

    def checklimit(self, v, limit, what):
        if v > limit:
            self.error(f"too many {what} (limit={limit})")

    # -- lcode.c

    def previous_instruction(self):
        fs = self.fs
        if fs.pc > fs.lasttarget:
            return fs.f.code[fs.pc - 1]
        return CREATE_0(OP_END)

    def deltastack(self, delta):
        fs = self.fs
        fs.stacklevel += delta
        if fs.stacklevel > fs.f.maxstacksize:
            if fs.stacklevel > MAXSTACK:
                self.error("function or expression too complex")
            fs.f.maxstacksize = fs.stacklevel

    def codelineinfo(self):
        fs, f = self.fs, self.fs.f
        if self.lastline > fs.lastline:
            if self.lastline > fs.lastline + 1:
                f.lineinfo.append(-(self.lastline - (fs.lastline + 1)))
            f.lineinfo.append(fs.pc)
            fs.lastline = self.lastline

    def erase_previous(self):
        """Drops the last instruction (a constant a jump made pointless) and its stack slot."""
        fs = self.fs
        fs.f.code.pop()
        fs.pc -= 1
        self.deltastack(-1)

    def code0(self, o):
        return self.code2(o, 0, 0)

    def code1(self, o, arg1):
        return self.code2(o, arg1, 0)

    def code2(self, o, arg1, arg2):
        fs = self.fs
        i = self.previous_instruction()
        mode, push, pop = PROPS[o]
        delta = push - pop
        optm = False
        po = GET_OPCODE(i)
        if o == OP_CLOSURE:
            delta = -arg2 + 1
        elif o == OP_SETTABLE:
            delta = -arg2
        elif o == OP_SETLIST:
            if arg2 == 0:
                return NO_JUMP
            delta = -arg2
        elif o == OP_SETMAP:
            if arg1 == 0:
                return NO_JUMP
            delta = -2 * arg1
        elif o == OP_RETURN:
            if po == OP_CALL and GETARG_B(i) == MULT_RET:
                i = SETARG_B(SET_OPCODE(i, OP_TAILCALL), arg1)
                optm = True
        elif o == OP_PUSHNIL:
            if arg1 == 0:
                return NO_JUMP
            delta = arg1
            if po == OP_PUSHNIL:
                i = SETARG_U(i, GETARG_U(i) + arg1)
                optm = True
        elif o == OP_POP:
            if arg1 == 0:
                return NO_JUMP
            delta = -arg1
            if po == OP_SETTABLE:
                i = SETARG_B(i, GETARG_B(i) + arg1)
                optm = True
        elif o == OP_GETTABLE:
            if po == OP_PUSHSTRING:      # `t.x'
                i = SET_OPCODE(i, OP_GETDOTTED)
                optm = True
            elif po == OP_GETLOCAL:      # `t[i]'
                i = SET_OPCODE(i, OP_GETINDEXED)
                optm = True
        elif o == OP_ADD:
            if po == OP_PUSHINT:         # `a+k'
                i = SET_OPCODE(i, OP_ADDI)
                optm = True
        elif o == OP_SUB:
            if po == OP_PUSHINT:         # `a-k'
                i = CREATE_S(OP_ADDI, -GETARG_S(i))
                optm = True
        elif o == OP_CONCAT:
            delta = -arg1 + 1
            if po == OP_CONCAT:          # `a..b..c'
                i = SETARG_U(i, GETARG_U(i) + 1)
                optm = True
        elif o == OP_MINUS:
            if po == OP_PUSHINT:         # `-k'
                i = SETARG_S(i, -GETARG_S(i))
                optm = True
            elif po == OP_PUSHNUM:       # `-k'
                i = SET_OPCODE(i, OP_PUSHNEGNUM)
                optm = True
        elif o == OP_JMPNE:
            if i == CREATE_U(OP_PUSHNIL, 1):    # `a~=nil'
                i = CREATE_S(OP_JMPT, NO_JUMP)
                optm = True
        elif o == OP_JMPEQ:
            if i == CREATE_U(OP_PUSHNIL, 1):    # `a==nil'
                i = CREATE_0(OP_NOT)
                delta = -1  # just undo effect of previous PUSHNIL
                optm = True
        elif o in (OP_JMPT, OP_JMPONT):
            if po == OP_NOT:
                i = CREATE_S(OP_JMPF, NO_JUMP)
                optm = True
            elif po == OP_PUSHINT and o == OP_JMPT:    # always jumps
                i = CREATE_S(OP_JMP, NO_JUMP)
                optm = True
            elif po == OP_PUSHNIL and GETARG_U(i) == 1:    # never jumps
                self.erase_previous()
                return NO_JUMP
        elif o in (OP_JMPF, OP_JMPONF):
            if po == OP_NOT:
                i = CREATE_S(OP_JMPT, NO_JUMP)
                optm = True
            elif po == OP_PUSHINT:    # `while 1 do': never jumps
                self.erase_previous()
                return NO_JUMP
            elif po == OP_PUSHNIL and GETARG_U(i) == 1:    # always jumps
                i = CREATE_S(OP_JMP, NO_JUMP)
                optm = True
        if optm:
            self.deltastack(delta)
            fs.f.code[fs.pc - 1] = i
            return fs.pc - 1
        self.deltastack(delta)
        if mode == iO:
            i = CREATE_0(o)
        elif mode == iU:
            i = CREATE_U(o, arg1)
        elif mode == iS:
            i = CREATE_S(o, arg1)
        else:
            i = CREATE_AB(o, arg1, arg2)
        self.codelineinfo()
        fs.f.code.append(i)
        fs.pc += 1
        return fs.pc - 1

    def kstr(self, c):
        self.code1(OP_PUSHSTRING, c)

    def number_constant(self, r):
        f = self.fs.f
        if self.number_size == 4:
            r = struct.unpack("<f", struct.pack("<f", r))[0]
        c = len(f.knum)
        lim = 0 if c < self.lookback else c - self.lookback
        c -= 1
        while c >= lim:
            if f.knum[c] == r:
                return c
            c -= 1
        f.knum.append(r)
        return len(f.knum) - 1

    def number(self, r):
        if r <= MAXARG_S and float(int(r)) == r:
            self.code1(OP_PUSHINT, int(r))
        else:
            self.code1(OP_PUSHNUM, self.number_constant(r))

    def adjuststack(self, n):
        if n > 0:
            self.code1(OP_POP, n)
        else:
            self.code1(OP_PUSHNIL, -n)

    def lastisopen(self) -> bool:
        i = self.previous_instruction()
        return GET_OPCODE(i) == OP_CALL and GETARG_B(i) == MULT_RET

    def setcallreturns(self, nresults):
        if self.lastisopen():
            fs = self.fs
            fs.f.code[fs.pc - 1] = SETARG_B(fs.f.code[fs.pc - 1], nresults)
            self.deltastack(nresults)

    def discharge(self, v) -> bool:
        if v.k == VLOCAL:
            self.code1(OP_GETLOCAL, v.index)
        elif v.k == VGLOBAL:
            self.code1(OP_GETGLOBAL, v.index)
        elif v.k == VINDEXED:
            self.code0(OP_GETTABLE)
        else:
            return False
        v.k = VEXP
        v.t = v.f = NO_JUMP
        return True

    def discharge1(self, v):
        self.discharge(v)
        if v.t == NO_JUMP and v.f == NO_JUMP:
            self.setcallreturns(1)

    def storevar(self, v):
        if v.k == VLOCAL:
            self.code1(OP_SETLOCAL, v.index)
        elif v.k == VGLOBAL:
            self.code1(OP_SETGLOBAL, v.index)
        elif v.k == VINDEXED:
            self.code2(OP_SETTABLE, 3, 3)
        else:
            raise AssertionError("invalid var kind to store")

    @staticmethod
    def invertjump(op):
        return {OP_JMPNE: OP_JMPEQ, OP_JMPEQ: OP_JMPNE, OP_JMPLT: OP_JMPGE, OP_JMPLE: OP_JMPGT,
                OP_JMPGT: OP_JMPLE, OP_JMPGE: OP_JMPLT, OP_JMPT: OP_JMPF, OP_JMPONT: OP_JMPF,
                OP_JMPF: OP_JMPT, OP_JMPONF: OP_JMPT}[op]

    def fixjump(self, pc, dest):
        code = self.fs.f.code
        if dest == NO_JUMP:
            code[pc] = SETARG_S(code[pc], NO_JUMP)
        else:
            offset = dest - (pc + 1)
            if abs(offset) > MAXARG_S:
                self.error("control structure too long")
            code[pc] = SETARG_S(code[pc], offset)

    def getjump(self, pc):
        offset = GETARG_S(self.fs.f.code[pc])
        return NO_JUMP if offset == NO_JUMP else pc + 1 + offset

    def jump(self):
        fs = self.fs
        j = self.code1(OP_JMP, NO_JUMP)
        if j == fs.lasttarget:  # possible jumps to this jump?
            j = self.concat(j, fs.jlt)
            fs.jlt = NO_JUMP
        return j

    def getlabel(self):
        fs = self.fs
        if fs.pc != fs.lasttarget:
            lasttarget = fs.lasttarget
            fs.lasttarget = fs.pc
            self.patchlist(fs.jlt, lasttarget)
            fs.jlt = NO_JUMP
        return fs.pc

    def patchlistaux(self, lst, target, special, special_target):
        code = self.fs.f.code
        while lst != NO_JUMP:
            nxt = self.getjump(lst)
            op = GET_OPCODE(code[lst])
            if op == special:
                self.fixjump(lst, special_target)
            else:
                self.fixjump(lst, target)
                if op == OP_JMPONT:
                    code[lst] = SET_OPCODE(code[lst], OP_JMPT)
                elif op == OP_JMPONF:
                    code[lst] = SET_OPCODE(code[lst], OP_JMPF)
            lst = nxt

    def patchlist(self, lst, target):
        fs = self.fs
        if target == fs.lasttarget:
            fs.jlt = self.concat(fs.jlt, lst)
        else:
            self.patchlistaux(lst, target, OP_END, 0)

    def need_value(self, lst, hasvalue) -> bool:
        while lst != NO_JUMP:
            if GET_OPCODE(self.fs.f.code[lst]) != hasvalue:
                return True
            lst = self.getjump(lst)
        return False

    def concat(self, l1, l2):
        """Returns the new head of list l1 with l2 appended."""
        if l1 == NO_JUMP:
            return l2
        lst = l1
        while True:
            nxt = self.getjump(lst)
            if nxt == NO_JUMP:
                self.fixjump(lst, l2)
                return l1
            lst = nxt

    def testgo(self, v, invert, jump):
        self.discharge1(v)
        fs = self.fs
        prevpos = fs.pc - 1
        previous = fs.f.code[prevpos]
        if not ISJUMP(GET_OPCODE(previous)):
            prevpos = self.code1(jump, NO_JUMP)
        elif invert:
            fs.f.code[prevpos] = SET_OPCODE(previous, self.invertjump(GET_OPCODE(previous)))
        if not invert:
            v.t = self.concat(v.t, prevpos)
            self.patchlist(v.f, self.getlabel())
            v.f = NO_JUMP
        else:
            v.f = self.concat(v.f, prevpos)
            self.patchlist(v.t, self.getlabel())
            v.t = NO_JUMP

    def goiftrue(self, v, keepvalue):
        self.testgo(v, True, OP_JMPONF if keepvalue else OP_JMPF)

    def goiffalse(self, v, keepvalue):
        self.testgo(v, False, OP_JMPONT if keepvalue else OP_JMPT)

    def code_label(self, op, arg):
        self.getlabel()
        return self.code1(op, arg)

    def tostack(self, v, onlyone):
        fs = self.fs
        if self.discharge(v):
            return
        previous = GET_OPCODE(fs.f.code[fs.pc - 1]) if fs.pc > 0 else OP_END
        if not ISJUMP(previous) and v.f == NO_JUMP and v.t == NO_JUMP:
            if onlyone:
                self.setcallreturns(1)
            return
        j = p_nil = p_1 = NO_JUMP
        if ISJUMP(previous) or self.need_value(v.f, OP_JMPONF) or self.need_value(v.t, OP_JMPONT):
            if ISJUMP(previous):
                v.t = self.concat(v.t, fs.pc - 1)
            else:
                j = self.code_label(OP_JMP, NO_JUMP)
                self.adjuststack(1)
            p_nil = self.code_label(OP_PUSHNILJMP, 0)
            p_1 = self.code_label(OP_PUSHINT, 1)
            self.patchlist(j, self.getlabel())
        final = self.getlabel()
        self.patchlistaux(v.f, p_nil, OP_JMPONF, final)
        self.patchlistaux(v.t, p_1, OP_JMPONT, final)
        v.f = v.t = NO_JUMP

    def prefix(self, op, v):
        fs = self.fs
        if op == OPR_MINUS:
            self.tostack(v, True)
            self.code0(OP_MINUS)
        else:
            self.discharge1(v)
            prev = fs.f.code[fs.pc - 1]
            if ISJUMP(GET_OPCODE(prev)):
                fs.f.code[fs.pc - 1] = SET_OPCODE(prev, self.invertjump(GET_OPCODE(prev)))
            else:
                self.code0(OP_NOT)
            v.f, v.t = v.t, v.f

    def infix(self, op, v):
        if op == OPR_AND:
            self.goiftrue(v, True)
        elif op == OPR_OR:
            self.goiffalse(v, True)
        else:
            self.tostack(v, True)

    def posfix(self, op, v1, v2):
        if op == OPR_AND:
            self.discharge1(v2)
            v1.t = v2.t
            v1.f = self.concat(v1.f, v2.f)
        elif op == OPR_OR:
            self.discharge1(v2)
            v1.f = v2.f
            v1.t = self.concat(v1.t, v2.t)
        else:
            self.tostack(v2, True)
            self.code1(*CODES[op])

    # -- lparser.c: variables and functions

    def string_constant(self, fs, s: bytes):
        f = fs.f
        c = f.kstr_index.get(s)
        if c is None:
            f.kstr.append(s)
            c = f.kstr_index[s] = len(f.kstr) - 1
        return c

    def code_string(self, s):
        self.kstr(self.string_constant(self.fs, s))

    def str_checkname(self) -> bytes:
        self.check_condition(self.t == TK_NAME, "<name> expected")
        ts = self.seminfo
        self.next()
        return ts

    def checkname(self):
        return self.string_constant(self.fs, self.str_checkname())

    def new_localvar(self, name: bytes, n):
        fs = self.fs
        self.checklimit(fs.nactloc + n + 1, MAXLOCALS, "local variables")
        fs.f.locvars.append([name, 0, 0])
        idx = len(fs.f.locvars) - 1
        while len(fs.actloc) <= fs.nactloc + n:
            fs.actloc.append(0)
        fs.actloc[fs.nactloc + n] = idx

    def adjustlocalvars(self, nvars):
        fs = self.fs
        for _ in range(nvars):
            fs.f.locvars[fs.actloc[fs.nactloc]][1] = fs.pc
            fs.nactloc += 1

    def removelocalvars(self, nvars):
        fs = self.fs
        for _ in range(nvars):
            fs.nactloc -= 1
            fs.f.locvars[fs.actloc[fs.nactloc]][2] = fs.pc

    def search_local(self, n: bytes, var):
        fs = self.fs
        level = 0
        while fs is not None:
            for i in range(fs.nactloc - 1, -1, -1):
                if n == fs.f.locvars[fs.actloc[i]][0]:
                    var.k = VLOCAL
                    var.index = i
                    return level
            level += 1
            fs = fs.prev
        var.k = VGLOBAL
        return -1

    def singlevar(self, n: bytes, var):
        level = self.search_local(n, var)
        if level >= 1:
            self.error(f"cannot access a variable in outer scope; `{n.decode('latin-1')}'")
        elif level == -1:
            var.index = self.string_constant(self.fs, n)

    def indexupvalue(self, v):
        fs = self.fs
        for i, u in enumerate(fs.upvalues):
            if u.k == v.k and u.index == v.index:
                return i
        self.checklimit(fs.nupvalues + 1, MAXUPVALUES, "upvalues")
        fs.upvalues.append(v.copy())
        fs.nupvalues += 1
        return fs.nupvalues - 1

    def pushupvalue(self, n: bytes):
        fs = self.fs
        v = ExpDesc()
        level = self.search_local(n, v)
        if level == -1:
            if fs.prev is None:
                self.error(f"cannot access upvalue in main; `{n.decode('latin-1')}'")
            v.index = self.string_constant(fs.prev, n)
        elif level != 1:
            self.error(f"upvalue must be global or local to immediately outer scope; `{n.decode('latin-1')}'")
        self.code1(OP_PUSHUPVALUE, self.indexupvalue(v))

    def adjust_mult_assign(self, nvars, nexps):
        diff = nexps - nvars
        if nexps > 0 and self.lastisopen():
            diff -= 1
            if diff <= 0:
                self.setcallreturns(-diff)
                diff = 0
            else:
                self.setcallreturns(0)
        self.adjuststack(diff)

    def code_params(self, nparams, dots):
        fs = self.fs
        self.adjustlocalvars(nparams)
        self.checklimit(fs.nactloc, MAXPARAMS, "parameters")
        fs.f.numparams = fs.nactloc
        fs.f.is_vararg = dots
        if dots:
            self.new_localvar(b"arg", 0)
            self.adjustlocalvars(1)
        self.deltastack(fs.nactloc)

    def enterbreak(self):
        fs = self.fs
        fs.bl = Breaklabel(fs.stacklevel, fs.bl)
        return fs.bl

    def leavebreak(self, bl):
        fs = self.fs
        fs.bl = bl.previous
        self.patchlist(bl.breaklist, self.getlabel())

    def func_onstack(self, func):
        fs = self.fs
        for u in func.upvalues:
            self.tostack(u.copy(), True)
        fs.f.kproto.append(func.f)
        self.code2(OP_CLOSURE, len(fs.f.kproto) - 1, func.nupvalues)

    def open_func(self):
        fs = FuncState(self.fs, self.source_name)
        self.fs = fs
        return fs

    def close_func(self):
        fs = self.fs
        self.code0(OP_END)
        self.getlabel()
        self.removelocalvars(fs.nactloc)
        fs.f.lineinfo.append(MAX_INT)
        self.fs = fs.prev

    def parse(self) -> Proto:
        fs = self.open_func()
        self.next()
        self.chunk()
        self.check_condition(self.t == TK_EOS, "<eof> expected")
        self.close_func()
        return fs.f

    # -- grammar

    def explist1(self):
        n = 1
        v = ExpDesc()
        self.expr(v)
        while self.t == ord(","):
            self.tostack(v, True)
            self.next()
            v = ExpDesc()
            self.expr(v)
            n += 1
        self.tostack(v, False)
        return n

    def funcargs(self, slf):
        fs = self.fs
        slevel = fs.stacklevel - slf - 1
        if self.t == ord("("):
            line = self.lx.linenumber
            self.next()
            if self.t != ord(")"):
                self.explist1()
            self.check_match(ord(")"), ord("("), line)
        elif self.t == ord("{"):
            self.constructor()
        elif self.t == TK_STRING:
            self.code_string(self.seminfo)
            self.next()
        else:
            self.error("function arguments expected")
        fs.stacklevel = slevel
        self.code2(OP_CALL, slevel, MULT_RET)

    def var_or_func_tail(self, v):
        while True:
            t = self.t
            if t == ord("."):
                self.next()
                self.tostack(v, True)
                self.kstr(self.checkname())
                v.k = VINDEXED
            elif t == ord("["):
                self.next()
                self.tostack(v, True)
                v.k = VINDEXED
                self.exp1()
                self.check(ord("]"))
            elif t == ord(":"):
                self.next()
                name = self.checkname()
                self.tostack(v, True)
                self.code1(OP_PUSHSELF, name)
                self.funcargs(1)
                v.k = VEXP
                v.t = v.f = NO_JUMP
            elif t in (ord("("), TK_STRING, ord("{")):
                self.tostack(v, True)
                self.funcargs(0)
                v.k = VEXP
                v.t = v.f = NO_JUMP
            else:
                return

    def var_or_func(self, v):
        if self.optional(ord("%")):
            self.pushupvalue(self.str_checkname())
            v.k = VEXP
            v.t = v.f = NO_JUMP
        else:
            self.singlevar(self.str_checkname(), v)
        self.var_or_func_tail(v)

    def recfield(self):
        if self.t == TK_NAME:
            self.kstr(self.checkname())
        elif self.t == ord("["):
            self.next()
            self.exp1()
            self.check(ord("]"))
        else:
            self.error("<name> or `[' expected")
        self.check(ord("="))
        self.exp1()

    def recfields(self):
        n = 1
        self.recfield()
        while self.t == ord(","):
            self.next()
            if self.t in (ord(";"), ord("}")):
                break
            self.recfield()
            n += 1
            if n % self.rfields == 0:
                self.code1(OP_SETMAP, self.rfields)
        self.code1(OP_SETMAP, n % self.rfields)
        return n

    def listfields(self):
        n = 1
        self.exp1()
        while self.t == ord(","):
            self.next()
            if self.t in (ord(";"), ord("}")):
                break
            self.exp1()
            n += 1
            self.checklimit(n // self.lfields, MAXARG_A, "`item groups' in a list initializer")
            if n % self.lfields == 0:
                self.code2(OP_SETLIST, n // self.lfields - 1, self.lfields)
        self.code2(OP_SETLIST, n // self.lfields, n % self.lfields)
        return n

    def constructor_part(self):
        """(n, kind) - kind: 0 list, 1 record, or the token that ended an empty part."""
        t = self.t
        if t in (ord(";"), ord("}")):
            return 0, t
        if t == TK_NAME:
            self.lookahead()
            if self.ahead[0] != ord("="):
                return self.listfields(), 0
            return self.recfields(), 1
        if t == ord("["):
            return self.recfields(), 1
        return self.listfields(), 0

    def constructor(self):
        fs = self.fs
        line = self.lx.linenumber
        pc = self.code1(OP_CREATETABLE, 0)
        self.check(ord("{"))
        n, k = self.constructor_part()
        nelems = n
        if self.optional(ord(";")):
            n2, k2 = self.constructor_part()
            self.check_condition(k != k2, "invalid constructor syntax")
            nelems += n2
        self.check_match(ord("}"), ord("{"), line)
        self.checklimit(nelems, MAXARG_U, "elements in a table constructor")
        fs.f.code[pc] = SETARG_U(fs.f.code[pc], nelems)

    def simpleexp(self, v):
        t = self.t
        if t == TK_NUMBER:
            r = self.seminfo
            self.next()
            self.number(r)
        elif t == TK_STRING:
            self.code_string(self.seminfo)
            self.next()
        elif t == TK_NIL:
            self.adjuststack(-1)
            self.next()
        elif t == ord("{"):
            self.constructor()
        elif t == TK_FUNCTION:
            self.next()
            self.body(False, self.lx.linenumber)
        elif t == ord("("):
            self.next()
            self.expr(v)
            self.check(ord(")"))
            if v.k == VEXP and v.t == NO_JUMP and v.f == NO_JUMP and self.lastisopen():
                # `(f())` gives one value, as in later Lua versions (stock 4.0 ignores the
                # parentheses) - the game's own scripts contain calls compiled that way
                self.setcallreturns(1)
            return
        elif t in (TK_NAME, ord("%")):
            self.var_or_func(v)
            return
        else:
            self.error("<expression> expected")
        v.k = VEXP
        v.t = v.f = NO_JUMP

    def exp1(self):
        v = ExpDesc()
        self.expr(v)
        self.tostack(v, True)

    def subexpr(self, v, limit):
        uop = {TK_NOT: OPR_NOT, ord("-"): OPR_MINUS}.get(self.t, OPR_NOUNOPR)
        if uop != OPR_NOUNOPR:
            self.next()
            self.subexpr(v, UNARY_PRIORITY)
            self.prefix(uop, v)
        else:
            self.simpleexp(v)
        op = _BINOPR.get(self.t, OPR_NOBINOPR)
        while op != OPR_NOBINOPR and PRIORITY[op][0] > limit:
            v2 = ExpDesc()
            self.next()
            self.infix(op, v)
            nextop = self.subexpr(v2, PRIORITY[op][1])
            self.posfix(op, v, v2)
            op = nextop
        return op

    def expr(self, v):
        self.subexpr(v, -1)

    # -- statements

    @staticmethod
    def block_follow(token):
        return token in (TK_ELSE, TK_ELSEIF, TK_END, TK_UNTIL, TK_EOS)

    def block(self):
        fs = self.fs
        nactloc = fs.nactloc
        self.chunk()
        self.adjuststack(fs.nactloc - nactloc)
        self.removelocalvars(fs.nactloc - nactloc)

    def assignment(self, v, nvars):
        left = 0
        self.checklimit(nvars, MAXVARSLH, "variables in a multiple assignment")
        if self.t == ord(","):
            nv = ExpDesc()
            self.next()
            self.var_or_func(nv)
            self.check_condition(nv.k != VEXP, "syntax error")
            left = self.assignment(nv, nvars + 1)
        else:
            self.check(ord("="))
            nexps = self.explist1()
            self.adjust_mult_assign(nvars, nexps)
        if v.k != VINDEXED:
            self.storevar(v)
        else:
            self.code2(OP_SETTABLE, left + nvars + 2, 1)
            left += 2
        return left

    def cond(self, v):
        self.expr(v)
        self.goiftrue(v, False)

    def whilestat(self, line):
        fs = self.fs
        while_init = self.getlabel()
        v = ExpDesc()
        bl = self.enterbreak()
        self.next()
        self.cond(v)
        self.check(TK_DO)
        self.block()
        self.patchlist(self.jump(), while_init)
        self.patchlist(v.f, self.getlabel())
        self.check_match(TK_END, TK_WHILE, line)
        self.leavebreak(bl)

    def repeatstat(self, line):
        repeat_init = self.getlabel()
        v = ExpDesc()
        bl = self.enterbreak()
        self.next()
        self.block()
        self.check_match(TK_UNTIL, TK_REPEAT, line)
        self.cond(v)
        self.patchlist(v.f, repeat_init)
        self.leavebreak(bl)

    def forbody(self, nvar, prepfor, loopfor):
        prep = self.code1(prepfor, NO_JUMP)
        blockinit = self.getlabel()
        self.check(TK_DO)
        self.adjustlocalvars(nvar)
        self.block()
        self.patchlist(self.code1(loopfor, NO_JUMP), blockinit)
        self.patchlist(prep, self.getlabel())
        self.removelocalvars(nvar)

    def fornum(self, varname):
        self.check(ord("="))
        self.exp1()
        self.check(ord(","))
        self.exp1()
        if self.optional(ord(",")):
            self.exp1()
        else:
            self.code1(OP_PUSHINT, 1)
        self.new_localvar(varname, 0)
        self.new_localvar(b"(limit)", 1)
        self.new_localvar(b"(step)", 2)
        self.forbody(3, OP_FORPREP, OP_FORLOOP)

    def forlist(self, indexname):
        self.check(ord(","))
        valname = self.str_checkname()
        self.check_condition(self.t == TK_NAME and self.seminfo == b"in", "`in' expected")
        self.next()
        self.exp1()
        self.new_localvar(b"(table)", 0)
        self.new_localvar(indexname, 1)
        self.new_localvar(valname, 2)
        self.forbody(3, OP_LFORPREP, OP_LFORLOOP)

    def forstat(self, line):
        bl = self.enterbreak()
        self.next()
        varname = self.str_checkname()
        if self.t == ord("="):
            self.fornum(varname)
        elif self.t == ord(","):
            self.forlist(varname)
        else:
            self.error("`=' or `,' expected")
        self.check_match(TK_END, TK_FOR, line)
        self.leavebreak(bl)

    def test_then_block(self, v):
        self.next()
        self.cond(v)
        self.check(TK_THEN)
        self.block()

    def ifstat(self, line):
        v = ExpDesc()
        escapelist = NO_JUMP
        self.test_then_block(v)
        while self.t == TK_ELSEIF:
            escapelist = self.concat(escapelist, self.jump())
            self.patchlist(v.f, self.getlabel())
            v = ExpDesc()
            self.test_then_block(v)
        if self.t == TK_ELSE:
            escapelist = self.concat(escapelist, self.jump())
            self.patchlist(v.f, self.getlabel())
            self.next()
            self.block()
        else:
            escapelist = self.concat(escapelist, v.f)
        self.patchlist(escapelist, self.getlabel())
        self.check_match(TK_END, TK_IF, line)

    def localnamelist(self):
        i = 1
        self.new_localvar(self.str_checkname(), 0)
        while self.t == ord(","):
            self.next()
            self.new_localvar(self.str_checkname(), i)
            i += 1
        return i

    def decinit(self):
        if self.t == ord("="):
            self.next()
            return self.explist1()
        return 0

    def localstat(self):
        self.next()
        nvars = self.localnamelist()
        nexps = self.decinit()
        self.adjust_mult_assign(nvars, nexps)
        self.adjustlocalvars(nvars)

    def funcname(self, v):
        needself = False
        self.singlevar(self.str_checkname(), v)
        if self.t in (ord(":"), ord(".")):
            needself = self.t == ord(":")
            self.next()
            self.tostack(v, True)
            self.kstr(self.checkname())
            v.k = VINDEXED
        return needself

    def funcstat(self, line):
        v = ExpDesc()
        self.next()
        needself = self.funcname(v)
        self.body(needself, line)
        self.storevar(v)

    def namestat(self):
        fs = self.fs
        v = ExpDesc()
        self.var_or_func(v)
        if v.k == VEXP:
            self.check_condition(self.lastisopen(), "syntax error")
            self.setcallreturns(0)
        else:
            left = self.assignment(v, 1)
            self.adjuststack(left)

    def retstat(self):
        fs = self.fs
        self.next()
        if not self.block_follow(self.t) and self.t != ord(";"):
            self.explist1()
        self.code1(OP_RETURN, fs.nactloc)
        fs.stacklevel = fs.nactloc

    def breakstat(self):
        fs = self.fs
        currentlevel = fs.stacklevel
        bl = fs.bl
        if bl is None:
            self.error("no loop to break")
        self.next()
        self.adjuststack(currentlevel - bl.stacklevel)
        bl.breaklist = self.concat(bl.breaklist, self.jump())
        self.adjuststack(bl.stacklevel - currentlevel)

    def stat(self) -> bool:
        line = self.lx.linenumber
        t = self.t
        if t == TK_IF:
            self.ifstat(line)
        elif t == TK_WHILE:
            self.whilestat(line)
        elif t == TK_DO:
            self.next()
            self.block()
            self.check_match(TK_END, TK_DO, line)
        elif t == TK_FOR:
            self.forstat(line)
        elif t == TK_REPEAT:
            self.repeatstat(line)
        elif t == TK_FUNCTION:
            self.funcstat(line)
        elif t == TK_LOCAL:
            self.localstat()
        elif t in (TK_NAME, ord("%")):
            self.namestat()
        elif t == TK_RETURN:
            self.retstat()
            return True
        elif t == TK_BREAK:
            self.breakstat()
            return True
        else:
            self.error("<statement> expected")
        return False

    def parlist(self):
        nparams = 0
        dots = 0
        if self.t != ord(")"):
            while True:
                if self.t == TK_DOTS:
                    self.next()
                    dots = 1
                elif self.t == TK_NAME:
                    self.new_localvar(self.str_checkname(), nparams)
                    nparams += 1
                else:
                    self.error("<name> or `...' expected")
                if dots or not self.optional(ord(",")):
                    break
        self.code_params(nparams, dots)

    def body(self, needself, line):
        new_fs = self.open_func()
        new_fs.f.line_defined = line
        self.check(ord("("))
        if needself:
            self.new_localvar(b"self", 0)
            self.adjustlocalvars(1)
        self.parlist()
        self.check(ord(")"))
        self.chunk()
        self.check_match(TK_END, TK_FUNCTION, line)
        self.close_func()
        self.func_onstack(new_fs)

    def chunk(self):
        islast = False
        while not islast and not self.block_follow(self.t):
            islast = self.stat()
            self.optional(ord(";"))


# --- dump (ldump.c) -------------------------------------------------------------------------

TEST_NUMBER = 3.14159265358979323846e8


def dump(f: Proto, size_t: int = 4, number_size: int = 4, strip: bool = True) -> bytes:
    st = "<I" if size_t == 4 else "<Q"
    nf = "<f" if number_size == 4 else "<d"
    out = bytearray(b"\x1bLua\x40\x01" + bytes([4, size_t, 4, 32, SIZE_OP, SIZE_B, number_size]))
    out += struct.pack(nf, TEST_NUMBER)

    def string(s):
        if s is None:
            out.extend(struct.pack(st, 0))
        else:
            out.extend(struct.pack(st, len(s) + 1) + s + b"\0")

    def function(p: Proto):
        string(b"=(none)" if strip else p.source)  # luac -s replaces the source name too
        out.extend(struct.pack("<iiBi", p.line_defined, p.numparams, p.is_vararg, p.maxstacksize))
        if strip:
            out.extend(struct.pack("<ii", 0, 0))
        else:
            out.extend(struct.pack("<i", len(p.locvars)))
            for name, startpc, endpc in p.locvars:
                string(name)
                out.extend(struct.pack("<ii", startpc, endpc))
            out.extend(struct.pack("<i", len(p.lineinfo)))
            out.extend(struct.pack(f"<{len(p.lineinfo)}i", *p.lineinfo))
        out.extend(struct.pack("<i", len(p.kstr)))
        for s in p.kstr:
            string(s)
        out.extend(struct.pack("<i", len(p.knum)))
        for n in p.knum:
            out.extend(struct.pack(nf, n))
        out.extend(struct.pack("<i", len(p.kproto)))
        for sub in p.kproto:
            function(sub)
        out.extend(struct.pack("<i", len(p.code)))
        out.extend(struct.pack(f"<{len(p.code)}I", *p.code))

    function(f)
    return bytes(out)


def lua_compile(source, name: str = "=(none)", strip: bool = True, size_t: int = 4, number_size: int = 4,
            fields_per_flush: int = None) -> bytes:
    """Lua 4.0 source (str or bytes) -> a bytecode chunk. The defaults are the
    game's format; size_t=8, number_size=8 is stock luac 4.0's.
    Raises LuaSyntaxError with the line number on a mistake."""
    if isinstance(source, str):
        source = source.encode("latin-1", "replace")
    if fields_per_flush is None:  # the game's format unless asked for stock luac's
        fields_per_flush = GAME_LFIELDS_PER_FLUSH if number_size == 4 else LFIELDS_PER_FLUSH
    c = Compiler(source, name.encode("latin-1"), number_size, fields_per_flush)
    return dump(c.parse(), size_t, number_size, strip)


# ==================================================================================================
# bf1_lua_decompile - merged in from bf1_lua_decompile.py
# ==================================================================================================
# bf1_lua_decompile.py - Lua 4.0 bytecode -> Lua source, for editing the
# game's scripts (scr_ chunks; see bf1_core.decode_script_chunk).
#
# The scripts are compiled Lua 4.0 (signature "\x1bLua\x40") with debug
# information stripped: no local variable names, no line numbers. Locals and
# parameters get made-up names (v3, p1, ...); everything else - globals,
# fields, strings, numbers - is exact.
#
# The decompiler is written against bf1_lua_compile, a port of Lua 4.0.1's own
# compiler: it reverses that code generator's patterns (how `and`/`or`
# chains, loops, breaks, multiple assignment, constructors, ... are laid out)
# by re-reading the instruction stream as the compiler wrote it. Every result
# can be checked: verify() compiles the source again and compares it with the
# original, instruction for instruction and constant for constant. The editor
# only offers a script for editing when that check passes.
#
#   decompile(bytecode) -> str          raises LuaDecompileError
#   verify(bytecode, source) -> bool    does `source` compile back to `bytecode`?


LUA_MAGIC = b"\x1bLua\x40"
LUA_NUMBER_CONST = 3.14159265358979323846e8


class LuaDecompileError(Exception):
    pass


# --- bytecode reader ----------------------------------------------------------------------

@dataclass
class ChunkHeader:
    is_little_endian: bool
    bytes_for_int: int
    bytes_for_size_t: int
    bytes_for_instruction: int
    bits_for_instruction: int
    bits_for_operator: int
    bits_for_register_b: int
    bytes_for_test_number: int
    test_number: float


@dataclass
class Local:
    name: str
    start_pc: int
    end_pc: int


@dataclass
class LuaFunction:
    name: str = ""
    line_defined: int = 0
    number_of_params: int = 0
    is_variadic: bool = False
    max_stack_size: int = 0
    instructions: list = field(default_factory=list)
    numbers: list = field(default_factory=list)
    globals: list = field(default_factory=list)      # the string constants
    locals: list = field(default_factory=list)
    lines: list = field(default_factory=list)
    functions: list = field(default_factory=list)


class _Reader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def bytes(self, n: int) -> bytes:
        chunk = self.data[self.pos:self.pos + n]
        if len(chunk) != n:
            raise LuaDecompileError(f"Unexpected end of bytecode at offset {self.pos}.")
        self.pos += n
        return chunk

    def u8(self) -> int:
        return self.bytes(1)[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.bytes(4))[0]

    def sized_uint(self, size: int) -> int:
        return int.from_bytes(self.bytes(size), "little")

    def number(self, size: int) -> float:
        fmt = {4: "<f", 8: "<d"}.get(size)
        if fmt is None:
            raise LuaDecompileError(f"Unsupported number size: {size} bytes.")
        return struct.unpack(fmt, self.bytes(size))[0]

    def string(self, size_t_bytes: int) -> str:
        length = self.sized_uint(size_t_bytes)
        return self.bytes(length)[:-1].decode("latin-1") if length else ""


def lua_read_header(r: _Reader) -> ChunkHeader:
    if r.bytes(5) != LUA_MAGIC:
        raise LuaDecompileError("Not Lua 4.0 bytecode (missing the '\\x1bLua\\x40' signature).")
    h = ChunkHeader(r.u8() == 1, r.u8(), r.u8(), r.u8(), r.u8(), r.u8(), r.u8(), 0, 0.0)
    h.bytes_for_test_number = r.u8()
    h.test_number = r.number(h.bytes_for_test_number)
    if not h.is_little_endian:
        raise LuaDecompileError("Big-endian Lua bytecode isn't supported.")
    if (h.bits_for_operator, h.bits_for_register_b) != (6, 9):
        raise LuaDecompileError("Unexpected instruction layout.")
    return h


def lua_read_function(r: _Reader, h: ChunkHeader) -> LuaFunction:
    fn = LuaFunction()
    fn.name = r.string(h.bytes_for_size_t)
    fn.line_defined = r.i32()
    fn.number_of_params = r.i32()
    fn.is_variadic = r.u8() == 1
    fn.max_stack_size = r.i32()
    for _ in range(r.i32()):
        fn.locals.append(Local(r.string(h.bytes_for_size_t), r.i32(), r.i32()))
    for _ in range(r.i32()):
        fn.lines.append(r.i32())
    for _ in range(r.i32()):
        fn.globals.append(r.string(h.bytes_for_size_t))
    for _ in range(r.i32()):
        fn.numbers.append(r.number(h.bytes_for_test_number))
    for _ in range(r.i32()):
        fn.functions.append(lua_read_function(r, h))
    for _ in range(r.i32()):
        fn.instructions.append(r.sized_uint(h.bytes_for_instruction))
    return fn


def lua_read_chunk(data: bytes) -> tuple:
    r = _Reader(data)
    h = lua_read_header(r)
    return h, lua_read_function(r, h)


def lua_chunk_length(data: bytes) -> int:
    """How many bytes the Lua chunk itself takes (the game's scr_ bodies add a zero byte)."""
    r = _Reader(data)
    lua_read_function(r, lua_read_header(r))
    return r.pos


def is_lua4_bytecode(data: bytes) -> bool:
    return data[:5] == LUA_MAGIC


OPCODES = {i: n for i, n in enumerate(
    "END RETURN CALL TAILCALL PUSHNIL POP PUSHINT PUSHSTRING PUSHNUM PUSHNEGNUM PUSHUPVALUE GETLOCAL GETGLOBAL "
    "GETTABLE GETDOTTED GETINDEXED PUSHSELF CREATETABLE SETLOCAL SETGLOBAL SETTABLE SETLIST SETMAP ADD ADDI SUB "
    "MULT DIV POW CONCAT MINUS NOT JMPNE JMPEQ JMPLT JMPLE JMPGT JMPGE JMPT JMPF JMPONT JMPONF JMP PUSHNILJMP "
    "FORPREP FORLOOP LFORPREP LFORLOOP CLOSURE".split())}
globals().update({"OP_" + n: i for i, n in OPCODES.items()})
MAXARG_S = ((1 << 26) - 1) >> 1
MULT_RET = 255
CMP_OPS = {OP_JMPNE: "~=", OP_JMPEQ: "==", OP_JMPLT: "<", OP_JMPLE: "<=", OP_JMPGT: ">", OP_JMPGE: ">="}  # noqa: F821
CMP_INVERSE = {"~=": "==", "==": "~=", "<": ">=", "<=": ">", ">": "<=", ">=": "<"}
COND_JUMPS = set(CMP_OPS) | {OP_JMPT, OP_JMPF, OP_JMPONT, OP_JMPONF}  # noqa: F821


def _op(i):
    return i & 0x3F


def _u(i):
    return i >> 6


def _s(i):
    return (i >> 6) - MAXARG_S


def _a(i):
    return i >> 15


def _b(i):
    return (i >> 6) & 0x1FF


# --- expressions ----------------------------------------------------------------------------
# Each node renders itself; `prio` is its binding strength (Lua 4.0's operator
# priorities), used to decide where parentheses are needed.

LUA_NAME_RESERVED = {"and", "break", "do", "else", "elseif", "end", "for", "function", "if", "local", "nil", "not",
            "or", "repeat", "return", "then", "until", "while"}
PRIO = {"or": 1, "and": 1, "==": 2, "~=": 2, "<": 2, "<=": 2, ">": 2, ">=": 2, "..": 4, "+": 5, "-": 5,
        "*": 6, "/": 6, "^": 9}
RIGHT_ASSOC = {"..", "^"}
UNARY_PRIO = 7
ATOM = 100


def is_name(s: str) -> bool:
    return bool(s) and (s[0].isalpha() or s[0] == "_") and all(c.isalnum() or c == "_" for c in s) \
        and s not in LUA_NAME_RESERVED and s.isascii()


def quote(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif o < 32 or o == 127:
            out.append(f"\\{o:03d}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


class Expr:
    prio = ATOM


class Nil(Expr):
    def render(self, d):
        return "nil"


class Num(Expr):
    def __init__(self, value, size=4):
        self.value, self.size = value, size
        self.prio = UNARY_PRIO if value < 0 or (value == 0 and str(value).startswith("-")) else ATOM

    def render(self, d):
        return fmt_number(self.value, self.size)


class Str(Expr):
    def __init__(self, s):
        self.s = s

    def render(self, d):
        return quote(self.s)


class Name(Expr):
    """A global, a local or an upvalue (%name) - just text."""
    def __init__(self, text):
        self.text = text

    def render(self, d):
        return self.text


class Index(Expr):
    def __init__(self, table, key):
        self.table, self.key = table, key

    def render(self, d):
        t = wrap(self.table, ATOM, d) if not isinstance(self.table, (Name, Index, Call)) else self.table.render(d)
        if isinstance(self.key, Str) and is_name(self.key.s):
            return f"{t}.{self.key.s}"
        return f"{t}[{self.key.render(d)}]"


class Call(Expr):
    def __init__(self, func, args, method=None):
        self.func, self.args, self.method = func, args, method

    def render(self, d):
        if self.method is not None:
            obj = self.func
            head = (obj.render(d) if isinstance(obj, (Name, Index, Call)) else f"({obj.render(d)})") + ":" + self.method
        else:
            f = self.func
            head = f.render(d) if isinstance(f, (Name, Index, Call)) else f"({f.render(d)})"
        return f"{head}({explist(self.args, d)})"


class Results(Expr):
    """One of several values a call returns (`local a, b = f()`)."""
    def __init__(self, call, index):
        self.call, self.index = call, index

    def render(self, d):
        return self.call.render(d)


class Binop(Expr):
    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b
        self.prio = PRIO[op]

    def render(self, d):
        p = self.prio
        right = self.op in RIGHT_ASSOC
        left = wrap(self.a, p + (1 if right else 0), d)
        rgt = wrap(self.b, p + (0 if right else 1), d)
        return f"{left} {self.op} {rgt}"


class Concat(Expr):
    prio = PRIO[".."]

    def __init__(self, parts):
        self.parts = parts

    def render(self, d):
        # a .. b .. c is one CONCAT 3; any part that is itself a concat needs parentheses
        return " .. ".join(wrap(p, PRIO[".."] + 1, d) for p in self.parts)


class Unop(Expr):
    prio = UNARY_PRIO

    def __init__(self, op, a):
        self.op, self.a = op, a

    def render(self, d):
        inner = wrap(self.a, UNARY_PRIO, d)
        return f"not {inner}" if self.op == "not" else f"-{inner}"


class Table(Expr):
    def __init__(self):
        self.parts = []   # [("list", [exprs]) or ("map", [(k, v)])]

    def add(self, kind, items):
        if self.parts and self.parts[-1][0] == kind:
            self.parts[-1][1].extend(items)
        else:
            self.parts.append((kind, list(items)))

    def size(self):
        return sum(len(items) for _k, items in self.parts)

    def render(self, d):
        if not self.parts:
            return "{}"
        rendered = []
        for kind, items in self.parts:
            if kind == "list":
                rendered.append(", ".join(e.render(d) for e in items))
            else:
                fields = []
                for k, v in items:
                    if isinstance(k, Str) and is_name(k.s):
                        fields.append(f"{k.s} = {v.render(d)}")
                    else:
                        fields.append(f"[{k.render(d)}] = {v.render(d)}")
                rendered.append(", ".join(fields))
        long = sum(len(r) for r in rendered) > 60 or "\n" in "".join(rendered)
        if not long:
            return "{" + "; ".join(rendered) + "}"
        pad = "  " * (d + 1)
        lines = []
        for n, (kind, items) in enumerate(self.parts):
            if kind == "list":
                lines += [pad + e.render(d + 1) + "," for e in items]
            else:
                for k, v in items:
                    key = k.s if isinstance(k, Str) and is_name(k.s) else f"[{k.render(d + 1)}]"
                    lines.append(f"{pad}{key} = {v.render(d + 1)},")
            if n + 1 < len(self.parts):
                lines[-1] = lines[-1][:-1] + ";"
        return "{\n" + "\n".join(lines) + "\n" + "  " * d + "}"


class Function(Expr):
    def __init__(self, decompiler, params, body):
        self.dec, self.params, self.body = decompiler, params, body

    def render(self, d):
        return f"function({', '.join(self.params)})\n{render_block(self.body, d + 1)}{'  ' * d}end"


class Cond(Expr):
    """and/or (same priority in Lua 4.0, left-associative)."""
    def __init__(self, op, a, b):
        self.op, self.a, self.b = op, a, b
        self.prio = 1

    def render(self, d):
        return f"{wrap(self.a, 1, d)} {self.op} {wrap(self.b, 2, d)}"


def wrap(e, need, d):
    s = e.render(d)
    return f"({s})" if e.prio < need else s


def fmt_number(v: float, size: int = 4) -> str:
    if v != v or v in (float("inf"), float("-inf")):
        raise LuaDecompileError("A number constant is NaN or infinite - can't be written as Lua source.")
    if v == int(v) and abs(v) < 1e15:
        return str(int(v))
    pack = "<f" if size == 4 else "<d"
    for digits in range(1, 18):
        s = f"{v:.{digits}g}"
        if struct.unpack(pack, struct.pack(pack, float(s)))[0] == v:
            break
    return s


# --- statements -----------------------------------------------------------------------------

class Stmt:
    pass


class LocalStmt(Stmt):
    def __init__(self, names, exprs):
        self.names, self.exprs = names, exprs


class Assign(Stmt):
    def __init__(self, targets, exprs):
        self.targets, self.exprs = targets, exprs


class CallStmt(Stmt):
    def __init__(self, call):
        self.call = call


class Return(Stmt):
    def __init__(self, exprs):
        self.exprs = exprs


class Break(Stmt):
    pass


class If(Stmt):
    def __init__(self, cond, then, orelse, has_else=None):
        self.cond, self.then, self.orelse = cond, then, orelse
        self.has_else = bool(orelse) if has_else is None else has_else


class While(Stmt):
    def __init__(self, cond, body):
        self.cond, self.body = cond, body


class Repeat(Stmt):
    def __init__(self, body, cond):
        self.body, self.cond = body, cond


class ForNum(Stmt):
    def __init__(self, var, start, limit, step, body):
        self.var, self.start, self.limit, self.step, self.body = var, start, limit, step, body


class ForIn(Stmt):
    def __init__(self, key, value, table, body):
        self.key, self.value, self.table, self.body = key, value, table, body


class Do(Stmt):
    def __init__(self, body):
        self.body = body


def explist(exprs, d) -> str:
    """Renders a value list; the results of one call are written once, and a call at the
    end that gives just one value is put in parentheses (otherwise it would give all)."""
    out, i = [], 0
    while i < len(exprs):
        e = exprs[i]
        if isinstance(e, Results):
            while i + 1 < len(exprs) and isinstance(exprs[i + 1], Results) and exprs[i + 1].call is e.call:
                i += 1
        text = e.render(d)
        if i == len(exprs) - 1 and isinstance(e, Call) and getattr(e, "nresults", None) == 1:
            text = f"({text})"
        out.append(text)
        i += 1
    return ", ".join(out)


def render_block(stmts, d) -> str:
    return "".join(render_stmt(s, d) for s in stmts)


def render_stmt(s, d) -> str:
    pad = "  " * d
    if isinstance(s, LocalStmt):
        return f"{pad}local {', '.join(s.names)}" + (f" = {explist(s.exprs, d)}" if s.exprs else "") + "\n"
    if isinstance(s, Assign):
        if len(s.targets) == 1 and len(s.exprs) == 1 and isinstance(s.exprs[0], Function):
            t = s.targets[0]
            fn = s.exprs[0]
            if isinstance(t, GlobalName) or (isinstance(t, Index) and isinstance(t.table, GlobalName)
                                             and isinstance(t.key, Str) and is_name(t.key.s)):
                return (f"{pad}function {t.render(d)}({', '.join(fn.params)})\n"
                        f"{render_block(fn.body, d + 1)}{pad}end\n")
        return f"{pad}{', '.join(t.render(d) for t in s.targets)} = {explist(s.exprs, d)}\n"
    if isinstance(s, CallStmt):
        return f"{pad}{s.call.render(d)}\n"
    if isinstance(s, Return):
        return f"{pad}return" + (f" {explist(s.exprs, d)}" if s.exprs else "") + "\n"
    if isinstance(s, Break):
        return f"{pad}break\n"
    if isinstance(s, If):
        out = f"{pad}if {s.cond.render(d)} then\n{render_block(s.then, d + 1)}"
        orelse, has_else = s.orelse, s.has_else
        while orelse and len(orelse) == 1 and isinstance(orelse[0], If):
            inner = orelse[0]
            out += f"{pad}elseif {inner.cond.render(d)} then\n{render_block(inner.then, d + 1)}"
            orelse, has_else = inner.orelse, inner.has_else
        if has_else:
            out += f"{pad}else\n{render_block(orelse, d + 1)}"
        return out + f"{pad}end\n"
    if isinstance(s, While):
        return f"{pad}while {s.cond.render(d) if s.cond else '1'} do\n{render_block(s.body, d + 1)}{pad}end\n"
    if isinstance(s, Repeat):
        return f"{pad}repeat\n{render_block(s.body, d + 1)}{pad}until {s.cond.render(d)}\n"
    if isinstance(s, ForNum):
        step = "" if s.step is None else f", {s.step.render(d)}"
        return (f"{pad}for {s.var} = {s.start.render(d)}, {s.limit.render(d)}{step} do\n"
                f"{render_block(s.body, d + 1)}{pad}end\n")
    if isinstance(s, ForIn):
        return f"{pad}for {s.key}, {s.value} in {s.table.render(d)} do\n{render_block(s.body, d + 1)}{pad}end\n"
    if isinstance(s, Do):
        return f"{pad}do\n{render_block(s.body, d + 1)}{pad}end\n"
    raise LuaDecompileError(f"Can't write a {type(s).__name__}.")


# --- the decompiler -------------------------------------------------------------------------

class _Fail(LuaDecompileError):
    pass


PEND, DECL, GARBAGE = "pending", "local", "garbage"


class LocalName(Name):
    def __init__(self, text):
        super().__init__(text)
        self.local = True


class GlobalName(Name):
    def __init__(self, text):
        super().__init__(text)
        self.local = False


class Atom:
    """A tested value and the conditional jump that tests it."""
    def __init__(self, expr, op, target, start, b=None):
        self.expr, self.op, self.target, self.start, self.b = expr, op, target, start, b


class FuncDecompiler:
    def __init__(self, fn: LuaFunction, depth: int, upvalues: list, number_size: int, taken: set):
        self.fn = fn
        self.code = fn.instructions
        self.depth = depth
        self.upvalues = upvalues          # names for PUSHUPVALUE, from the enclosing function
        self.nsize = number_size
        self.taken = taken                 # every string constant in the chunk - local names avoid them
        self.stack = []                    # expression per stack slot
        self.state = []                    # PEND / DECL / GARBAGE per slot
        self.names = {}                    # slot -> local name in scope
        self.L = 0                         # declared locals
        self.backjumps = {}                # target -> [positions of jumps to it from later]
        self.active_heads = set()          # loop starts being decoded (their body starts there too)
        self.counter = 0                   # locals named so far
        for pc, ins in enumerate(self.code):
            o = _op(ins)
            if o in COND_JUMPS or o in (OP_JMP, OP_FORPREP, OP_FORLOOP, OP_LFORPREP, OP_LFORLOOP):  # noqa: F821
                t = pc + 1 + _s(ins)
                if t <= pc:
                    self.backjumps.setdefault(t, []).append(pc)

    # -- names

    def local_name(self, slot, kind="v"):
        """A fresh name for a local (the originals were stripped): v1, v2, ... in the order
        they're declared, p1, p2, ... for parameters. Never a global or string the chunk
        uses, nor an outer local this function reads as an upvalue."""
        if kind == "p":
            base = f"p{slot + 1}"
        else:
            self.counter += 1
            base = f"{kind}{self.counter}"
        while base in self.taken or base in self.upvalues:
            base += "_"
        return base

    # -- small helpers

    def target(self, pc):
        return pc + 1 + _s(self.code[pc])

    def resolve(self, t):
        seen = set()
        while 0 <= t < len(self.code) and _op(self.code[t]) == OP_JMP and t not in seen:  # noqa: F821
            seen.add(t)
            t = self.target(t)
        return t

    def same(self, a, b):
        return a == b or self.resolve(a) == self.resolve(b)

    def push(self, e):
        self.stack.append(e)
        self.state.append(PEND)

    def pop(self):
        if len(self.stack) <= self.L:
            raise _Fail("stack underflow")
        self.state.pop()
        return self.stack.pop()

    def popn(self, n):
        if n == 0:
            return []
        items = self.stack[-n:]
        if len(self.stack) - n < self.L:
            raise _Fail("stack underflow")
        del self.stack[-n:]
        del self.state[-n:]
        return items

    def const_str(self, k):
        return self.fn.globals[k]

    # -- locals

    def declare(self, upto, out):
        """Slots L..upto-1 (pending values) become locals: emits a `local` statement."""
        if upto <= self.L:
            return
        while upto < len(self.stack) and isinstance(self.stack[upto], Results) and \
                isinstance(self.stack[upto - 1], Results) and self.stack[upto].call is self.stack[upto - 1].call:
            upto += 1
        if any(s != PEND for s in self.state[self.L:upto]):
            raise _Fail("local declaration over a non-value")
        names, exprs = [], []
        for slot in range(self.L, upto):
            name = self.local_name(slot)
            names.append(name)
            e = self.stack[slot]
            exprs.append(e)
            self.names[slot] = name
            self.stack[slot] = LocalName(name)
            self.state[slot] = DECL
            # a call's several results only expand at the end of a list: start a new `local` after them
            nxt = self.stack[slot + 1] if slot + 1 < upto else None
            if isinstance(e, Results) and nxt is not None and not (isinstance(nxt, Results) and nxt.call is e.call):
                out.append(LocalStmt(names, exprs))
                names, exprs = [], []
        if names:
            out.append(LocalStmt(names, exprs))
        self.L = upto

    def declare_pending(self, out):
        self.declare(len(self.stack), out)

    def local_ref(self, slot, out):
        if slot >= self.L:
            self.declare(slot + 1, out)
        return LocalName(self.names[slot])

    # -- the main loop

    def decompile(self):
        fn = self.fn
        params = []
        for slot in range(fn.number_of_params):
            name = self.local_name(slot, "p")
            self.names[slot] = name
            params.append(name)
        nlocals = fn.number_of_params
        if fn.is_variadic:
            self.names[nlocals] = "arg"
            params.append("...")
            nlocals += 1
        self.stack = [LocalName(self.names[s]) for s in range(nlocals)]
        self.state = [DECL] * nlocals
        self.L = nlocals
        end = len(self.code)
        if not self.code or _op(self.code[-1]) != OP_END:  # noqa: F821
            raise _Fail("function doesn't end with END")
        body = self.block(0, end - 1, loop_exit=None)
        return params, body

    def block(self, pc, end, loop_exit, top_level_pop_ok=True):
        """Statements for instructions [pc, end)."""
        out = []
        base_L = self.L
        while pc < end:
            pc = self.statement(pc, end, out, loop_exit, base_L)
        self.declare_pending(out)
        return out

    def statement(self, pc, end, out, loop_exit, base_L):
        """Decodes from pc; returns the next pc."""
        # loops start at a label that a later jump goes back to
        backs = [j for j in self.backjumps.get(pc, []) if j < end]
        if backs and pc not in self.active_heads:
            self.declare_pending(out)  # values pushed before a loop are locals declared before it
            loop = self.try_loop(pc, end, backs, out, loop_exit)
            if loop is not None:
                return loop
        ins = self.code[pc]
        o = _op(ins)
        if o == OP_POP:  # noqa: F821
            return self.on_pop(pc, end, out, loop_exit, base_L)
        if o == OP_JMP:  # noqa: F821
            t = self.target(pc)
            if loop_exit is not None and self.same(t, loop_exit):
                self.declare_pending(out)
                out.append(Break() if pc + 1 >= end else Do([Break()]))
                return pc + 1
            if t > pc:  # `if nil then`, `if nil and x then`: the constant test compiles to a bare jump
                return self.if_statement(pc, end, out, loop_exit)
            raise _Fail(f"unexpected jump at {pc}")
        if o in (OP_FORPREP, OP_LFORPREP):  # noqa: F821
            return self.for_loop(pc, end, out, loop_exit)
        if o in COND_JUMPS and not self.is_value_jump(pc):
            return self.if_statement(pc, end, out, loop_exit)
        if o in (OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE):  # noqa: F821
            return self.assignment(pc, out, extra=[], end=end, base_L=base_L, loop_exit=loop_exit)
        if o == OP_CALL and _b(ins) == 0:  # noqa: F821
            a = _a(ins)
            call = self.make_call(a, out)
            self.declare(a, out)
            if len(self.stack) != a:
                raise _Fail("call statement leaves values")
            self.declare_pending(out)
            out.append(CallStmt(call))
            return pc + 1
        if o == OP_RETURN:  # noqa: F821
            u = _u(ins)
            vals = self.stack[u:]
            self.declare(u, out)
            if len(self.stack) < u:
                raise _Fail("return below locals")
            del self.stack[u:], self.state[u:]
            self.declare_pending(out)
            out.append(self.last_statement(Return(vals), pc, end))
            return pc + 1
        if o == OP_TAILCALL:  # noqa: F821
            a = _a(ins)
            call = self.make_call(a, out)
            self.declare(a, out)
            del self.stack[a:], self.state[a:]
            self.declare_pending(out)
            out.append(self.last_statement(Return([call]), pc, end))
            return pc + 1
        return self.expression_step(pc, out)

    def last_statement(self, stmt, pc, end):
        """return/break must end a block: one with code after it was written `do return end`
        (or `if 1 then return end`, which compiles the same)."""
        rest = [j for j in range(pc + 1, end) if _op(self.code[j]) != OP_POP]  # noqa: F821
        return Do([stmt]) if rest else stmt

    # -- expressions

    def expression_step(self, pc, out):
        """One expression instruction (pushes/pops values). Returns the next pc."""
        ins = self.code[pc]
        o = _op(ins)
        f = self.fn
        if o == OP_PUSHNIL:  # noqa: F821
            for _ in range(_u(ins)):
                self.push(Nil())
        elif o == OP_PUSHINT:  # noqa: F821
            self.push(Num(_s(ins), self.nsize))
        elif o == OP_PUSHSTRING:  # noqa: F821
            self.push(Str(self.const_str(_u(ins))))
        elif o == OP_PUSHNUM:  # noqa: F821
            self.push(Num(f.numbers[_u(ins)], self.nsize))
        elif o == OP_PUSHNEGNUM:  # noqa: F821
            self.push(Num(-f.numbers[_u(ins)], self.nsize))
        elif o == OP_PUSHUPVALUE:  # noqa: F821
            self.push(Name("%" + self.upvalues[_u(ins)]))
        elif o == OP_GETLOCAL:  # noqa: F821
            self.push(self.local_ref(_u(ins), out))
        elif o == OP_GETGLOBAL:  # noqa: F821
            self.push(GlobalName(self.const_str(_u(ins))))
        elif o == OP_GETTABLE:  # noqa: F821
            k = self.pop()
            t = self.pop()
            self.push(Index(t, k))
        elif o == OP_GETDOTTED:  # noqa: F821
            t = self.pop()
            self.push(Index(t, Str(self.const_str(_u(ins)))))
        elif o == OP_GETINDEXED:  # noqa: F821
            k = self.local_ref(_u(ins), out)
            t = self.pop()
            self.push(Index(t, k))
        elif o == OP_PUSHSELF:  # noqa: F821
            obj = self.pop()
            self.push(("method", obj, self.const_str(_u(ins))))
            self.push(("self",))
        elif o == OP_CREATETABLE:  # noqa: F821
            t = Table()
            t.expected = _u(ins)
            self.push(t)
        elif o == OP_SETLIST:  # noqa: F821
            n = _b(ins)
            items = self.popn(n)
            t = self.stack[-1]
            if not isinstance(t, Table):
                raise _Fail("SETLIST without a table")
            t.add("list", items)
        elif o == OP_SETMAP:  # noqa: F821
            n = _u(ins)
            items = self.popn(2 * n)
            t = self.stack[-1]
            if not isinstance(t, Table):
                raise _Fail("SETMAP without a table")
            t.add("map", [(items[2 * i], items[2 * i + 1]) for i in range(n)])
        elif o in (OP_ADD, OP_SUB, OP_MULT, OP_DIV, OP_POW):  # noqa: F821
            b = self.pop()
            a = self.pop()
            self.push(Binop({OP_ADD: "+", OP_SUB: "-", OP_MULT: "*", OP_DIV: "/", OP_POW: "^"}[o], a, b))  # noqa: F821
        elif o == OP_ADDI:  # noqa: F821
            a = self.pop()
            k = _s(ins)
            self.push(Binop("+", a, Num(k, self.nsize)) if k >= 0 else Binop("-", a, Num(-k, self.nsize)))
        elif o == OP_CONCAT:  # noqa: F821
            parts = self.popn(_u(ins))
            self.push(Concat(parts))
        elif o == OP_MINUS:  # noqa: F821
            self.push(Unop("-", self.pop()))
        elif o == OP_NOT:  # noqa: F821
            self.push(Unop("not", self.pop()))
        elif o == OP_CALL:  # noqa: F821
            a, b = _a(ins), _b(ins)
            call = self.make_call(a, out)
            if b == MULT_RET:
                self.push(call)
                self.state[-1] = PEND
                call.open = True
            elif b == 1:
                call.nresults = 1
                self.push(call)
            else:
                for i in range(b):
                    self.push(Results(call, i))
        elif o == OP_CLOSURE:  # noqa: F821
            self.push(self.closure(_a(ins), _b(ins)))
        elif o in COND_JUMPS:
            return self.value_region(pc, out)
        else:
            raise _Fail(f"unexpected {OPCODES.get(o)} at {pc}")
        return pc + 1

    def make_call(self, a, out):
        if a < self.L:
            raise _Fail("call below locals")
        if len(self.stack) <= a:
            raise _Fail("call without a function")
        func = self.stack[a]
        args = self.stack[a + 1:]
        if any(s != PEND for s in self.state[a:]):
            raise _Fail("call over non-values")
        del self.stack[a:], self.state[a:]
        if isinstance(func, tuple) and func[0] == "method":
            if not args or args[0] != ("self",):
                raise _Fail("method call without self")
            return Call(func[1], args[1:], method=func[2])
        if any(isinstance(x, tuple) for x in args):
            raise _Fail("stray self")
        return Call(func, args)

    def closure(self, index, nup):
        ups = self.popn(nup)
        names = []
        for u in ups:
            if isinstance(u, Name) and not u.text.startswith("%"):
                names.append(u.text)
            else:
                raise _Fail("upvalue that isn't a variable")
        sub = self.fn.functions[index]
        dec = FuncDecompiler(sub, self.depth + 1, names, self.nsize, self.taken)
        params, body = dec.decompile()
        return Function(dec, params, body)

    # -- and / or / comparisons as values

    def find_pushniljmp(self, pc):
        """The PUSHNILJMP; PUSHINT 1 pair that ends a value region started at pc, if any."""
        for q in range(pc + 1, len(self.code) - 1):
            if _op(self.code[q]) == OP_PUSHNILJMP and self.code[q + 1] == ((MAXARG_S + 1) << 6 | OP_PUSHINT):  # noqa: F821
                return q
        return None

    def region_end(self, pc):
        """(q, fin) when the conditional jump at pc belongs to a value region (`a and b`,
        a comparison used as a value, ...) rather than an if/while/until condition."""
        o = _op(self.code[pc])
        q = self.find_pushniljmp(pc)
        if o in (OP_JMPONT, OP_JMPONF):  # noqa: F821
            fin = self.target(pc)
            if q is not None and fin == q + 2:
                return q, fin
            return None, fin
        if q is None or not (pc < self.target(pc) <= q + 2):
            return None
        if not all(pc < self.target(j) <= q + 2 for j in range(pc, q) if _op(self.code[j]) in COND_JUMPS):
            return None
        if any(self.is_statement_op(j) for j in range(pc + 1, q) if not (j == q - 2 or j == q - 1)):
            return None
        return q, q + 2

    def is_statement_op(self, j):
        """An instruction only a statement produces - it can't sit inside an expression."""
        o = _op(self.code[j])
        return o in (OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE, OP_POP, OP_RETURN, OP_TAILCALL, OP_JMP,  # noqa: F821
                     OP_FORPREP, OP_LFORPREP, OP_FORLOOP, OP_LFORLOOP, OP_END) or \
            (o == OP_CALL and _b(self.code[j]) == 0)  # noqa: F821

    def is_value_jump(self, pc):
        return self.region_end(pc) is not None

    def value_region(self, pc, out):
        """Collects a value region's atoms, builds its expression and pushes it; returns the pc after."""
        found = self.region_end(pc)
        if found is None:
            raise _Fail(f"condition inside an expression at {pc}")
        q, fin = found
        if q is not None:
            plain_last = _op(self.code[q - 2]) == OP_JMP and self.target(q - 2) == fin  # noqa: F821
            limit = q - 2 if plain_last else q
        else:
            plain_last, limit = True, fin
        atoms = []
        cur, start = pc, None
        while cur < limit:
            op = _op(self.code[cur])
            if op in COND_JUMPS and (cur == pc or self.region_jump(cur, q, fin)):
                if op in CMP_OPS:
                    b = self.pop()
                    a = self.pop()
                    at = Atom(a, op, self.target(cur), start, b)
                else:
                    at = Atom(self.pop(), op, self.target(cur), start)
                at._pos = cur
                atoms.append(at)
                cur += 1
                start = cur
                continue
            cur = self.expression_step(cur, out)
        last = self.pop() if plain_last else None
        if q is not None:
            is_t = lambda a: a.target == q + 1 or (a.target == fin and a.op == OP_JMPONT)  # noqa: E731,F821
            is_f = lambda a: a.target == q or (a.target == fin and a.op == OP_JMPONF)  # noqa: E731,F821
        else:
            is_t = lambda a: a.target == fin and a.op == OP_JMPONT  # noqa: E731,F821
            is_f = lambda a: a.target == fin and a.op == OP_JMPONF  # noqa: E731,F821
        if q is not None:
            for a in atoms:
                a.direct = a.target in (q, q + 1)
        self.push(self.build(atoms, last, is_t, is_f, last_start=start if plain_last else None))
        return fin

    def region_jump(self, cur, q, fin):
        """Is this conditional jump one of the region's own (not a nested region's)?"""
        t = self.target(cur)
        if q is not None:
            return t in (q, q + 1, fin) or self.is_atom_start(t, cur)
        return t == fin or self.is_atom_start(t, cur)

    def is_atom_start(self, t, cur):
        """A jump to just after a later conditional jump of the same region (an internal and/or target)."""
        return t > cur + 1 and _op(self.code[t - 1]) in COND_JUMPS

    def build(self, atoms, last, is_t, is_f, last_start=None):
        """The and/or tree for atoms - each jumps to the 'true' exit, the 'false' exit or a
        later atom's start - followed by an optional plain last value. `and` and `or` share
        one priority in Lua 4.0 and group to the left, so the split is the rightmost atom
        whose jump leaves the whole left part."""
        items = atoms + ([last] if last is not None else [])

        def start(k):
            return atoms[k].start if k < len(atoms) else last_start

        def go(i, j, is_t, is_f):
            if i == j:
                it = items[i]
                if not isinstance(it, Atom):
                    return it
                return self.leaf(it, is_t(it), is_f(it))
            for k in range(j - 1, i - 1, -1):
                a = items[k]
                nxt = start(k + 1)
                inner = {start(m) for m in range(i + 1, k + 1)}

                def ok(test):
                    return all(test(items[m]) or items[m].target == nxt or items[m].target in inner
                               for m in range(i, k + 1))
                if is_f(a) and ok(is_f):
                    return Cond("and", go(i, k, lambda x, n=nxt: x.target == n, is_f), go(k + 1, j, is_t, is_f))
                if is_t(a) and ok(is_t):
                    return Cond("or", go(i, k, is_t, lambda x, n=nxt: x.target == n), go(k + 1, j, is_t, is_f))
            raise _Fail("can't untangle a condition")

        return go(0, len(items) - 1, is_t, is_f)

    def leaf(self, a, to_true, to_false):
        if not (to_true or to_false):
            raise _Fail("atom jumps nowhere known")
        if getattr(a, "const_jump", False):  # PUSHNIL+JMPF or PUSHINT+JMPT, folded into a JMP
            return Nil() if to_false else Num(1, self.nsize)
        if a.op in CMP_OPS:
            op = CMP_OPS[a.op]
            if to_false:
                op = CMP_INVERSE[op]
            return Binop(op, a.expr, a.b)
        jumps_if_true = a.op in (OP_JMPT, OP_JMPONT)  # noqa: F821
        test = a.expr
        if getattr(a, "direct", False) and a.op in (OP_JMPT, OP_JMPF):  # noqa: F821
            # a plain test jumping straight to the value pushes: only `x ~= nil` compiles to that
            test = Binop("~=", a.expr, Nil())
        # an empty `if` jumps to the same place either way: read it as the false exit
        positive = (not jumps_if_true) if to_false else jumps_if_true
        return test if positive else Unop("not", test)

    # -- statements

    def on_pop(self, pc, end, out, loop_exit, base_L):
        n = _u(self.code[pc])
        nxt = pc + 1
        # break: POP n; JMP exit; PUSHNIL n
        if nxt < len(self.code) and _op(self.code[nxt]) == OP_JMP and loop_exit is not None and \
                self.same(self.target(nxt), loop_exit):  # noqa: F821
            self.declare_pending(out)
            out.append(Break())
            after = nxt + 1
            if after < end and self.code[after] == (n << 6 | OP_PUSHNIL):  # noqa: F821
                after += 1
            return after
        # extra values of an assignment: POP n right before its stores
        if nxt < len(self.code) and _op(self.code[nxt]) in (OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE):  # noqa: F821
            extra = self.popn(n)
            return self.assignment(nxt, out, extra=extra, end=end, base_L=base_L, loop_exit=loop_exit)
        top = len(self.stack)
        pend = [s for s in range(self.L, top)]
        if len(pend) > n:
            # `local a = 1, 2`: the extra values of a local declaration
            keep = top - n
            extra = self.stack[keep:]
            self.declare(keep, out)
            out[-1].exprs = out[-1].exprs + extra
            del self.stack[keep:], self.state[keep:]
            return pc + 1
        # the end of a block of locals: declare what's pending, then they go out of scope
        self.declare_pending(out)
        self.end_scope(n, pc + 1 >= end, out, base_L)
        return pc + 1

    def end_scope(self, n, at_block_end, out, base_L):
        """n locals go out of scope. In the middle of a block that's a `do ... end`."""
        if n > self.L - base_L:
            raise _Fail("pop below the block's locals")
        first_slot = self.L - n
        if not at_block_end:
            # a `do ... end` block in the middle: wrap from where its first local was declared
            k = next((i for i, s in enumerate(out) if isinstance(s, LocalStmt) and s.names
                      and s.names[0] == self.names.get(first_slot)), None)
            if k is None:
                k = next(i for i in range(len(out) - 1, -1, -1) if isinstance(out[i], LocalStmt))
            inner = out[k:]
            del out[k:]
            out.append(Do(inner))
        del self.stack[first_slot:], self.state[first_slot:]
        self.L = first_slot

    def assignment(self, pc, out, extra, end, base_L, loop_exit=None):
        run = pc
        while run < len(self.code) and _op(self.code[run]) in (OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE):  # noqa: F821
            run += 1
        pairs = []
        scope_pops = 0
        for p in range(pc, run):
            ins = self.code[p]
            o = _op(ins)
            if o == OP_SETGLOBAL:  # noqa: F821
                v = self.pop()
                pairs.append((GlobalName(self.const_str(_u(ins))), v))
            elif o == OP_SETLOCAL:  # noqa: F821
                v = self.pop()
                slot = _u(ins)
                if slot >= self.L:
                    self.declare(slot + 1, out)
                pairs.append((LocalName(self.names[slot]), v))
            else:
                A, B = _a(ins), _b(ins)
                n = len(self.stack)
                ti = n - A
                if ti < self.L or ti + 1 >= n - 1:
                    raise _Fail("bad SETTABLE")
                t, k = self.stack[ti], self.stack[ti + 1]
                v = self.stack[-1]
                self.state[ti] = self.state[ti + 1] = GARBAGE
                del self.stack[-1], self.state[-1]
                pairs.append((Index(t, k), v))
                scope_pops += self.drop(B - 1)
        pc = run
        if pc < len(self.code) and _op(self.code[pc]) == OP_POP and self.state and self.state[-1] == GARBAGE:  # noqa: F821
            scope_pops += self.drop(_u(self.code[pc]))
            pc += 1
        if any(s == GARBAGE for s in self.state):
            raise _Fail("assignment left table/key values")
        pairs.reverse()
        targets = [t for t, _v in pairs]
        values = [v for _t, v in pairs] + extra
        self.declare_pending(out)
        out.append(Assign(targets, values))
        if scope_pops and pc < len(self.code) and _op(self.code[pc]) == OP_JMP and loop_exit is not None                 and self.same(self.target(pc), loop_exit):  # noqa: F821
            # `break` - its POP merged into the store; the locals stay in scope after it
            out.append(Break())
            pc += 1
            if pc < end and self.code[pc] == (scope_pops << 6 | OP_PUSHNIL):  # noqa: F821
                pc += 1
            return pc
        if scope_pops:  # the end of a block, merged into the store
            self.end_scope(scope_pops, pc >= end, out, base_L)
        return pc

    def drop(self, n):
        """Pops n slots after a store: the table/key garbage. Returns how many more it
        pops - locals whose block ends here (the block's POP merged into the store)."""
        for done in range(n):
            if not self.stack:
                raise _Fail("pop from empty stack")
            if self.state[-1] != GARBAGE:
                return n - done
            self.stack.pop()
            self.state.pop()
        return 0

    # -- if

    def eval_to_condition(self, cur, stop):
        """Evaluates a pure expression from cur up to the next condition jump (not one inside
        a value region); raises _Fail on anything that's a statement. Returns its pc."""
        sub = []
        while cur < stop:
            o = _op(self.code[cur])
            if o in COND_JUMPS and not self.is_value_jump(cur):
                break
            if o in (OP_SETLOCAL, OP_SETGLOBAL, OP_SETTABLE, OP_POP, OP_RETURN, OP_TAILCALL, OP_JMP,  # noqa: F821
                     OP_FORPREP, OP_LFORPREP, OP_END, OP_FORLOOP, OP_LFORLOOP) or \
                    (o == OP_CALL and _b(self.code[cur]) == 0):  # noqa: F821
                raise _Fail("statement")
            cur = self.expression_step(cur, sub)
            if sub:
                raise _Fail("declares a local")
        if cur >= stop:
            raise _Fail("no condition")
        return cur

    def snapshot(self, out=None):
        return (list(self.stack), list(self.state), self.L, dict(self.names), len(out) if out is not None else 0)

    def restore(self, snap, out=None):
        self.stack, self.state, self.L, self.names = list(snap[0]), list(snap[1]), snap[2], dict(snap[3])
        if out is not None:
            del out[snap[4]:]

    def gather_atoms(self, pc, out, max_atoms=None, stop=None):
        """Atoms from a statement-level conditional jump at pc onwards (greedy): [(Atom, jump_pc)]."""
        ins = self.code[pc]
        op = _op(ins)
        atoms = []

        def make(cur):
            op = _op(self.code[cur])
            if op == OP_JMP:  # noqa: F821
                at = Atom(Nil(), OP_JMPF, self.target(cur), None)  # noqa: F821
                at.const_jump = True
                at._pos = cur
                return at
            if op in CMP_OPS:
                b = self.pop()
                a = self.pop()
                at = Atom(a, op, self.target(cur), None, b)
            else:
                at = Atom(self.pop(), op, self.target(cur), None)
            at._pos = cur
            return at

        first = make(pc)
        self.declare_pending(out)
        first.start = None
        atoms.append(first)
        cur = pc + 1
        stop = len(self.code) if stop is None else stop
        while max_atoms is None or len(atoms) < max_atoms:
            if cur in self.backjumps:
                break
            snap = self.snapshot()
            depth = len(self.stack)
            try:
                j = self.eval_to_condition(cur, stop)
                if len(self.stack) == depth:
                    raise _Fail("nothing tested")
                at = make(j)
                if len(self.stack) != depth:
                    raise _Fail("unbalanced")
                at.start = cur
                atoms.append(at)
                cur = j + 1
            except _Fail:
                self.restore(snap)
                break
        return atoms

    def cond_tree(self, atoms, T, F):
        """The condition for atoms whose last jump goes to F when false and falls through to T."""
        def is_f(a):
            return self.same(a.target, F)

        def is_t(a):
            return a.target == T or (self.resolve(a.target) == self.resolve(T) and not self.same(a.target, F))
        self._last_start = None
        return self.build(atoms, None, is_t, is_f)

    def split_condition(self, atoms, limit, below=None):
        """Largest prefix of atoms (shorter than `below`) that forms a valid condition: (n, T, F)."""
        for n in range((below or len(atoms) + 1) - 1, 0, -1):
            if n > len(atoms):
                continue
            last = atoms[n - 1]
            T = last._pos + 1
            F = last.target
            starts = {a.start for a in atoms[1:n]}
            ok = all(self.same(a.target, F) or a.target == T or a.target in starts or
                     self.resolve(a.target) == self.resolve(T) for a in atoms[:n])
            if ok:
                return n, T, F
        raise _Fail("can't find the end of a condition")

    def effective(self, t, limit):
        """A jump target, pulled back to the end of the enclosing block when it's been
        threaded through the jump that ends that block."""
        if t <= limit:
            return t
        if limit < len(self.code) and self.resolve(limit) == self.resolve(t):
            return limit
        raise _Fail(f"jump past the enclosing block ({t} > {limit})")

    def if_statement(self, pc, end, out, loop_exit):
        snap = self.snapshot(out)
        atoms = self.gather_atoms(pc, out, stop=end)
        below = None
        while True:
            # the longest run of tests that reads as one condition (an empty `if` followed by
            # another `if` can look like a single condition until it's untangled)
            n, T, F = self.split_condition(atoms, end, below)
            if n < len(atoms):
                self.restore(snap, out)
                atoms = self.gather_atoms(pc, out, max_atoms=n, stop=end)
            try:
                cond = self.cond_tree(atoms, T, F)
                break
            except _Fail:
                if n == 1:
                    raise
                below = n
        Feff = end if F < T else self.effective(F, end)
        then_end, else_range = Feff, None
        if Feff - 1 >= T and _op(self.code[Feff - 1]) == OP_JMP:  # noqa: F821
            jt = self.target(Feff - 1)
            is_break = loop_exit is not None and self.same(jt, loop_exit)
            if not is_break and jt >= Feff:
                else_range = (Feff, self.effective(jt, end))
                then_end = Feff - 1
        L0 = self.L
        then = self.block(T, then_end, loop_exit)
        self.close_scope(L0)
        orelse = []
        if else_range is not None:
            orelse = self.block(else_range[0], else_range[1], loop_exit)
            self.close_scope(L0)
            out.append(If(cond, then, orelse, has_else=True))
            return else_range[1]
        out.append(If(cond, then, orelse))
        return then_end

    def close_scope(self, L0):
        if self.L != L0 or len(self.stack) != L0:
            # the block's own locals were popped by its last POP; anything else is wrong
            if len(self.stack) < L0:
                raise _Fail("block popped outer locals")
            del self.stack[L0:], self.state[L0:]
            self.L = L0

    # -- loops

    def try_loop(self, pc, end, backs, out, loop_exit):
        self.active_heads.add(pc)
        try:
            return self._try_loop(pc, end, backs, out, loop_exit)
        finally:
            self.active_heads.discard(pc)

    def _try_loop(self, pc, end, backs, out, loop_exit):
        jumps = [(j, _op(self.code[j])) for j in backs]
        uncond = [j for j, o in jumps if o == OP_JMP]  # noqa: F821
        if uncond:
            e = max(uncond)
            exit_ = e + 1
            L0 = self.L
            # condition?
            body_start = pc
            cond = None
            o = None
            probe = pc
            snapshot = (list(self.stack), list(self.state), self.L, dict(self.names))
            try:
                sub = []
                probe = self.eval_to_condition(probe, e)
                if len(self.stack) > len(snapshot[0]):
                    atoms = self.gather_atoms(probe, sub, stop=e)
                    n, T, F = self.split_condition(atoms, e)
                    if n == len(atoms) and self.same(F, exit_) and not sub:
                        cond = self.cond_tree(atoms, T, F)
                        body_start = T
                    else:
                        raise _Fail("not this loop's condition")
                else:
                    raise _Fail("no condition")
            except _Fail:
                self.stack, self.state, self.L, self.names = snapshot
                cond = None
                body_start = pc
            body = self.block(body_start, e, loop_exit=exit_)
            self.close_scope(L0)
            out.append(While(cond, body))
            return exit_
        # repeat ... until: conditional jumps back to pc
        e = max(j for j, _o in jumps)
        L0 = self.L
        stmts = []
        cur = pc
        while cur <= e:
            snapshot = (list(self.stack), list(self.state), self.L, dict(self.names))
            try:
                sub = []
                probe = self.eval_to_condition(cur, e + 1)
                if len(self.stack) == len(snapshot[0]):
                    raise _Fail("no value")
                atoms = self.gather_atoms(probe, sub, stop=e + 1)
                last = atoms[-1]
                if last._pos != e or sub:
                    raise _Fail("not the until")
                T = e + 1
                ok = all(self.same(a.target, pc) or a.target == T or a.target in {x.start for x in atoms[1:]}
                         for a in atoms)
                if not ok:
                    raise _Fail("not the until")
                cond = self.cond_tree(atoms, T, pc)
                self.close_scope(L0)
                out.append(Repeat(stmts, cond))
                return e + 1
            except _Fail:
                self.stack, self.state, self.L, self.names = snapshot
            cur = self.statement(cur, e + 1, stmts, e + 1, L0)
        raise _Fail("repeat without until")

    def for_loop(self, pc, end, out, loop_exit):
        ins = self.code[pc]
        o = _op(ins)
        # the loop instruction jumping back to the body start (the prep's own exit
        # jump may have been threaded on past it)
        back = OP_FORLOOP if o == OP_FORPREP else OP_LFORLOOP  # noqa: F821
        loop_at = next((j for j in range(pc + 1, len(self.code))
                        if _op(self.code[j]) == back and self.target(j) == pc + 1), None)
        if loop_at is None:
            raise _Fail("for without its loop")
        exit_ = loop_at + 1
        if o == OP_FORPREP:  # noqa: F821
            start, limit, step = self.popn(3)
            self.declare_pending(out)
            slot = len(self.stack)
            var = self.local_name(slot, "i")
            for k, nm in enumerate((var, "(limit)", "(step)")):
                self.stack.append(LocalName(nm))
                self.state.append(DECL)
                self.names[slot + k] = nm
            self.L += 3
            body = self.block(pc + 1, loop_at, loop_exit=exit_)
            self.close_scope(slot + 3)
            del self.stack[slot:], self.state[slot:]
            self.L = slot
            show_step = None if (isinstance(step, Num) and step.value == 1) else step
            out.append(ForNum(var, start, limit, show_step, body))
        else:
            table = self.pop()
            self.declare_pending(out)
            slot = len(self.stack)
            k, v = self.local_name(slot + 1, "k"), self.local_name(slot + 2, "v")
            for i, nm in enumerate(("(table)", k, v)):
                self.stack.append(LocalName(nm))
                self.state.append(DECL)
                self.names[slot + i] = nm
            self.L += 3
            body = self.block(pc + 1, loop_at, loop_exit=exit_)
            self.close_scope(slot + 3)
            del self.stack[slot:], self.state[slot:]
            self.L = slot
            out.append(ForIn(k, v, table, body))
        return exit_


def _all_strings(fn, acc):
    acc.update(fn.globals)
    for sub in fn.functions:
        _all_strings(sub, acc)
    return acc


def lua_decompile(data: bytes) -> str:
    """Lua source for a compiled Lua 4.0 chunk. Raises LuaDecompileError when a
    pattern isn't understood."""
    header, main = lua_read_chunk(data)
    taken = _all_strings(main, set())
    dec = FuncDecompiler(main, 0, [], header.bytes_for_test_number, taken)
    try:
        params, body = dec.decompile()
    except _Fail as exc:
        raise LuaDecompileError(f"Couldn't decompile: {exc}") from None
    except (IndexError, KeyError) as exc:
        raise LuaDecompileError(f"Couldn't decompile ({type(exc).__name__}: {exc})") from None
    return render_block(body, 0)


# --- checking -------------------------------------------------------------------------------

def _shape(fn: LuaFunction):
    """Everything that affects how a function runs (not its line numbers or names)."""
    return (fn.number_of_params, fn.is_variadic, fn.max_stack_size, tuple(fn.globals),
            tuple(fn.numbers), tuple(fn.instructions), tuple(_shape(s) for s in fn.functions))


def lua_same_program(a: bytes, b: bytes) -> bool:
    """Do two chunks run identically? Compares code, constants and stack sizes; ignores
    debug information (line numbers, local names) and where functions were defined."""
    try:
        ha, fa = lua_read_chunk(a)
        hb, fb = lua_read_chunk(b)
    except LuaDecompileError:
        return False
    return ha.bytes_for_test_number == hb.bytes_for_test_number and _shape(fa) == _shape(fb)


def lua_verify(data: bytes, source: str) -> bool:
    """Does `source` compile back to the same program as `data`?"""
    h, _main = lua_read_chunk(data)
    try:
        again = lua_compile(source, size_t=h.bytes_for_size_t, number_size=h.bytes_for_test_number)
    except LuaSyntaxError:
        return False
    return lua_same_program(data, again)


# ==================================================================================================
# bf1_anim - merged in from bf1_anim.py
# ==================================================================================================
# bf1_anim.py - read, edit and write the game's animations: 'zaa_' chunks
# (animation banks) and 'zaf_' chunks (the skeleton they play on).
#
# Both chunks are NAME + BIN_. BIN_ is a tree of blocks whose 4-byte tags are
# stored backwards ('SMNA' is ANMS), each followed by a u32 size. Joint and
# animation names are stored only as zero_crc(name).
#
#   zaa_  BIN_ = ANMS (size 0), then
#           u32 8, u32 joints (summed over animations), u32 ADAT size, u32 animations
#           ANIM  per animation (16 bytes): 8 bytes of flags, u32 name crc,
#                 u16 frames, u16 joints
#           AJNT  per animation, per joint (48 bytes): u32 joint crc,
#                 4 x u32 rotation channel offsets (x, y, z, w),
#                 3 x u32 position channel offsets, 3 x f32 position bias,
#                 f32 position scale
#           ADAT  the channels, at those byte offsets. A channel is an i16 first
#                 value, then one byte per step:
#                   -128 n    hold the value for n more frames
#                   -127 i16  jump to that value
#                   other     add it to the value (one frame)
#                 rotation = value / 2047, position = bias + value * scale
#
#   zaf_  BIN_ = ZAFF > VERS, SIZE, MTLS, JSTS, SKNS, SKEL, PXYS, SHDS
#           SKEL > JOINT per joint: u32 crc, quaternion (x, y, z, w),
#                  position (x, y, z), u32 parent, child and sibling crc
#
# The animation data is local to each joint's parent: a joint with no
# animation of its own still has a track, holding its rest pose.



ROT_SCALE = 2047.0
FRAME_RATE = 30  # frames per second the game plays animations at
_HOLD, _JUMP = -128, -127
_MAX_HOLD = 127


class AnimError(Exception):
    pass


def _crc_table() -> list:
    table = []
    for i in range(256):
        c = i << 24
        for _ in range(8):
            c = ((c << 1) ^ 0x04C11DB7) if c & 0x80000000 else c << 1
        table.append(c & 0xFFFFFFFF)
    return table


_CRC_TABLE = _crc_table()


def zero_crc(name: str) -> int:
    """The engine's hash for joint and animation names: CRC-32 (MSB-first,
    poly 0x04C11DB7) of the lowercased name."""
    c = 0xFFFFFFFF
    for b in name.lower().encode("ascii", "replace"):
        c = ((c << 8) & 0xFFFFFFFF) ^ _CRC_TABLE[((c >> 24) ^ b) & 0xFF]
    return c ^ 0xFFFFFFFF


# --- names --------------------------------------------------------------------------------

_anim_names: dict = {}


def anim_learn_names(names) -> None:
    for n in names:
        if n:
            _anim_names.setdefault(zero_crc(n), n)


def anim_name_of(crc: int) -> str:
    return _anim_names.get(crc) or f"0x{crc:08x}"


def crc_of(text: str) -> int:
    """A name, or a hash written as 0x... (how unnamed ones are shown)."""
    text = text.strip()
    if text.lower().startswith("0x"):
        return int(text, 16)
    anim_learn_names([text])
    return zero_crc(text)


def learn_from_container(container) -> None:
    """Names every joint the level's skeletons ('skel' chunks) mention."""
    for _path, _i, chunk, _names_path in find_in_container(container, tag_q="skel"):
        try:
            body = parse_body(chunk.payload)
        except Exception:
            continue
        for sub in body.chunks:
            if sub.tag in ("NAME", "PRNT", "INFO"):
                anim_learn_names(s.decode("ascii", "replace") for s in sub.payload.split(b"\0"))


# --- the reversed-tag blocks --------------------------------------------------------------

def _anim_blocks(data: bytes, pos: int = 0, end: int = None):
    end = len(data) if end is None else end
    while pos + 8 <= end:
        tag = data[pos:pos + 4][::-1].decode("ascii", "replace")
        size = struct.unpack_from("<I", data, pos + 4)[0]
        yield tag, pos + 8, size
        pos += 8 + size


def _anim_block(tag: str, payload: bytes) -> bytes:
    return tag.encode("ascii")[::-1] + struct.pack("<I", len(payload)) + payload


def _anim_bin_payload(chunk_payload: bytes) -> tuple:
    body = parse_body(chunk_payload)
    name = _find_child(body, "NAME")
    data = _find_child(body, "BIN_")
    if data is None:
        raise AnimError("No BIN_ block in this chunk.")
    return (decode_ascii(name.payload) if name else ""), data.payload


def _anim_chunk_payload(name: str, bin_data: bytes) -> bytes:
    return (tagged_block(b"NAME", name.encode("ascii") + b"\0")
            + tagged_block(b"BIN_", bin_data))


# --- channels -----------------------------------------------------------------------------

def decode_channel(data: bytes, pos: int, frames: int) -> list:
    value = struct.unpack_from("<h", data, pos)[0]
    pos += 2
    out = [value]
    while len(out) < frames:
        step = struct.unpack_from("<b", data, pos)[0]
        pos += 1
        if step == _HOLD:
            out.extend([value] * data[pos])
            pos += 1
        elif step == _JUMP:
            value = struct.unpack_from("<h", data, pos)[0]
            pos += 2
            out.append(value)
        else:
            value += step
            out.append(value)
    return out[:frames]


def encode_channel(values: list) -> bytes:
    out = bytearray(struct.pack("<h", values[0]))
    i, n = 1, len(values)
    while i < n:
        prev = values[i - 1]
        run = 0
        while i + run < n and values[i + run] == prev and run < _MAX_HOLD:
            run += 1
        if run >= 2:
            out += struct.pack("<bB", _HOLD, run)
            i += run
            continue
        step = values[i] - prev
        if -126 <= step <= 127:
            out += struct.pack("<b", step)
        else:
            out += struct.pack("<bh", _JUMP, values[i])
        i += 1
    return bytes(out)


def _i16(v: float) -> int:
    return max(-32768, min(32767, int(round(v))))


# --- zaa_ ---------------------------------------------------------------------------------

@dataclass
class Track:
    joint: int        # joint crc
    rot: list         # 4 channels (x, y, z, w), one i16 per frame, value / 2047
    pos: list         # 3 channels, one i16 per frame, bias + value * scale
    bias: tuple = (0.0, 0.0, 0.0)
    scale: float = 0.0

    def rotation(self, f: int) -> tuple:
        return tuple(ch[f] / ROT_SCALE for ch in self.rot)

    def position(self, f: int) -> tuple:
        return tuple(b + ch[f] * self.scale for b, ch in zip(self.bias, self.pos))

    @property
    def moves(self) -> bool:
        return any(len(set(ch)) > 1 for ch in self.rot + (self.pos if self.scale else []))


@dataclass
class Animation:
    crc: int
    frames: int
    tracks: list
    flags: bytes = bytes(8)

    @property
    def name(self) -> str:
        return anim_name_of(self.crc)

    @property
    def seconds(self) -> float:
        return max(self.frames - 1, 0) / FRAME_RATE

    def copy(self) -> "Animation":
        return Animation(self.crc, self.frames, [
            Track(t.joint, [list(c) for c in t.rot], [list(c) for c in t.pos], tuple(t.bias), t.scale)
            for t in self.tracks], bytes(self.flags))


@dataclass
class AnimBank:
    name: str
    animations: list = field(default_factory=list)
    version: int = 8

    def find(self, name: str) -> Animation:
        crc = crc_of(name)
        for a in self.animations:
            if a.crc == crc:
                return a
        raise AnimError(f"No animation '{name}' in {self.name}.")


def decode_zaa(chunk_payload: bytes) -> AnimBank:
    name, data = _anim_bin_payload(chunk_payload)
    if data[:4] != b"SMNA":
        raise AnimError("Not an animation bank (no ANMS block).")
    _zero, version, _joints, _adat_size, count = struct.unpack_from("<5I", data, 4)
    blocks = {tag: (start, size) for tag, start, size in _anim_blocks(data, 24)}
    try:
        anim_at, _ = blocks["ANIM"]
        joint_at, _ = blocks["AJNT"]
        adat_at, adat_size = blocks["ADAT"]
    except KeyError as missing:
        raise AnimError(f"Animation bank is missing its {missing} block.")
    adat = data[adat_at:adat_at + adat_size]
    bank = AnimBank(name, version=version)
    jp = joint_at
    for i in range(count):
        p = anim_at + 16 * i
        flags = data[p:p + 8]
        crc, frames, njoints = struct.unpack_from("<IHH", data, p + 8)
        tracks = []
        for _ in range(njoints):
            jcrc = struct.unpack_from("<I", data, jp)[0]
            offs = struct.unpack_from("<7I", data, jp + 4)
            bx, by, bz, scale = struct.unpack_from("<4f", data, jp + 32)
            jp += 48
            rot = [decode_channel(adat, o, frames) for o in offs[:4]]
            pos = [decode_channel(adat, o, frames) for o in offs[4:]] if scale else [[0] * frames for _ in range(3)]
            tracks.append(Track(jcrc, rot, pos, (bx, by, bz), scale))
        bank.animations.append(Animation(crc, frames, tracks, flags))
    return bank


def encode_zaa(bank: AnimBank) -> bytes:
    adat = bytearray()
    seen = {}

    def channel(values) -> int:
        raw = encode_channel(values)
        if raw not in seen:
            seen[raw] = len(adat)
            adat.extend(raw)
        return seen[raw]

    anim_block, joint_block = bytearray(), bytearray()
    total_joints = 0
    for a in bank.animations:
        if not 1 <= a.frames <= 0xFFFF:
            raise AnimError(f"{a.name}: {a.frames} frames is out of range.")
        anim_block += bytes(a.flags[:8]).ljust(8, b"\0") + struct.pack("<IHH", a.crc, a.frames, len(a.tracks))
        for t in a.tracks:
            rot = [channel(ch) for ch in t.rot]
            pos = [channel(ch) for ch in t.pos] if t.scale else [0, 0, 0]
            joint_block += struct.pack("<I7I4f", t.joint, *rot, *pos, *t.bias, t.scale)
        total_joints += len(a.tracks)
    adat_size = len(adat)
    adat.extend(bytes(-len(adat) % 4))
    data = (b"SMNA" + struct.pack("<5I", 0, bank.version, total_joints, adat_size, len(bank.animations))
            + _anim_block("ANIM", bytes(anim_block)) + _anim_block("AJNT", bytes(joint_block)) + _anim_block("ADAT", bytes(adat)))
    return _anim_chunk_payload(bank.name, data)


# --- zaf_ (skeleton) ----------------------------------------------------------------------

@dataclass
class Joint:
    crc: int
    rotation: tuple   # quaternion x, y, z, w (parent-local)
    position: tuple   # parent-local
    parent: int

    @property
    def name(self) -> str:
        return anim_name_of(self.crc)


def decode_zaf(chunk_payload: bytes) -> tuple:
    """(name, [Joint]) - the skeleton an animation bank plays on."""
    name, data = _anim_bin_payload(chunk_payload)
    joints = []

    def walk(start: int, end: int):
        for tag, pos, size in _anim_blocks(data, start, end):
            if tag == "JONT" and size >= 44:
                crc = struct.unpack_from("<I", data, pos)[0]
                q = struct.unpack_from("<4f", data, pos + 4)
                p = struct.unpack_from("<3f", data, pos + 20)
                parent = struct.unpack_from("<I", data, pos + 32)[0]
                joints.append(Joint(crc, q, p, parent))
            elif tag in ("ZAFF", "SKEL"):
                walk(pos, pos + size)

    walk(0, len(data))
    return name, joints


# --- maths for previews and edits ---------------------------------------------------------

def quat_mul(a, b) -> tuple:
    ax, ay, az, aw = a
    bx, by, bz, bw = b
    return (aw * bx + ax * bw + ay * bz - az * by,
            aw * by - ax * bz + ay * bw + az * bx,
            aw * bz + ax * by - ay * bx + az * bw,
            aw * bw - ax * bx - ay * by - az * bz)


def quat_rotate(q, v) -> tuple:
    x, y, z, w = q
    vx, vy, vz = v
    tx, ty, tz = 2 * (y * vz - z * vy), 2 * (z * vx - x * vz), 2 * (x * vy - y * vx)
    return (vx + w * tx + y * tz - z * ty, vy + w * ty + z * tx - x * tz, vz + w * tz + x * ty - y * tx)


def _normalize(q) -> tuple:
    n = math.sqrt(sum(c * c for c in q)) or 1.0
    return tuple(c / n for c in q)


def pose(skeleton: list, anim: Animation, frame: int) -> dict:
    """World position of every joint at a frame: {crc: (x, y, z)}, plus
    parent links - joints the animation has no track for keep their rest pose."""
    tracks = {t.joint: t for t in anim.tracks} if anim else {}
    by_crc = {j.crc: j for j in skeleton}
    world = {}

    def solve(crc):
        if crc in world:
            return world[crc]
        j = by_crc[crc]
        t = tracks.get(crc)
        rot = _normalize(t.rotation(frame)) if t else j.rotation
        pos = t.position(frame) if t and t.scale else j.position
        if j.parent in by_crc:
            prot, ppos = solve(j.parent)
            off = quat_rotate(prot, pos)
            result = (quat_mul(prot, rot), (ppos[0] + off[0], ppos[1] + off[1], ppos[2] + off[2]))
        else:
            result = (rot, tuple(pos))
        world[crc] = result
        return result

    for j in skeleton:
        solve(j.crc)
    return {crc: p for crc, (_r, p) in world.items()}


def _lerp_channel(ch: list, src: float) -> float:
    i = min(int(src), len(ch) - 1)
    k = min(i + 1, len(ch) - 1)
    f = src - i
    return ch[i] * (1 - f) + ch[k] * f


def retime(anim: Animation, frames: int) -> Animation:
    """The animation stretched or squeezed to `frames` frames: rotations are
    blended along the shortest path, positions linearly."""
    frames = max(1, int(frames))
    old = anim.frames
    srcs = [(i * (old - 1) / (frames - 1)) if frames > 1 else 0.0 for i in range(frames)]
    tracks = []
    for t in anim.tracks:
        rot = [[], [], [], []]
        for s in srcs:
            i = min(int(s), old - 1)
            k = min(i + 1, old - 1)
            f = s - i
            a = [t.rot[c][i] for c in range(4)]
            b = [t.rot[c][k] for c in range(4)]
            if sum(x * y for x, y in zip(a, b)) < 0:
                b = [-x for x in b]
            q = [x * (1 - f) + y * f for x, y in zip(a, b)]
            n = math.sqrt(sum(x * x for x in q)) or 1.0
            for c in range(4):
                rot[c].append(_i16(q[c] / n * ROT_SCALE))
        pos = [[_i16(_lerp_channel(ch, s)) for s in srcs] for ch in t.pos]
        tracks.append(Track(t.joint, rot, pos, tuple(t.bias), t.scale))
    return Animation(anim.crc, frames, tracks, bytes(anim.flags))


def scale_motion(anim: Animation, factor: float) -> Animation:
    """Exaggerates (factor > 1) or tones down (< 1) every joint's movement
    away from its first frame."""
    out = anim.copy()
    for t in out.tracks:
        first = _normalize(t.rotation(0))
        inv = (-first[0], -first[1], -first[2], first[3])
        for f in range(out.frames):
            q = _normalize(t.rotation(f))
            d = quat_mul(inv, q)
            if d[3] < 0:
                d = tuple(-c for c in d)
            angle = 2 * math.acos(max(-1.0, min(1.0, d[3])))
            s = math.sin(angle / 2)
            if s < 1e-6:
                continue
            axis = (d[0] / s, d[1] / s, d[2] / s)
            half = angle * factor / 2
            d2 = (axis[0] * math.sin(half), axis[1] * math.sin(half), axis[2] * math.sin(half), math.cos(half))
            q2 = quat_mul(first, d2)
            for c in range(4):
                t.rot[c][f] = _i16(q2[c] * ROT_SCALE)
        if t.scale:
            for ch in t.pos:
                base = ch[0]
                for f in range(out.frames):
                    ch[f] = _i16(base + (ch[f] - base) * factor)
    return out


# --- text form (export / import) ----------------------------------------------------------

def animation_to_dict(anim: Animation) -> dict:
    """An animation as plain numbers: per joint, per frame, the rotation
    quaternion (x, y, z, w) and position, both parent-local."""
    return {
        "name": anim.name, "frames": anim.frames, "fps": FRAME_RATE, "flags": anim.flags.hex(),
        "joints": [{
            "joint": anim_name_of(t.joint),
            "rotation": [[round(v, 5) for v in t.rotation(f)] for f in range(anim.frames)],
            **({"position": [[round(v, 5) for v in t.position(f)] for f in range(anim.frames)]} if t.scale else {}),
            **({"rest_position": [round(v, 5) for v in t.bias]} if not t.scale else {}),
        } for t in anim.tracks],
    }


def animation_from_dict(d: dict) -> Animation:
    frames = int(d["frames"])
    tracks = []
    for j in d["joints"]:
        rots = j["rotation"]
        if len(rots) != frames:
            raise AnimError(f"{j['joint']}: {len(rots)} rotations for {frames} frames.")
        rot = [[_i16(r[c] * ROT_SCALE) for r in rots] for c in range(4)]
        if "position" in j:
            ps = j["position"]
            if len(ps) != frames:
                raise AnimError(f"{j['joint']}: {len(ps)} positions for {frames} frames.")
            lo = [min(p[c] for p in ps) for c in range(3)]
            hi = [max(p[c] for p in ps) for c in range(3)]
            bias = tuple((l + h) / 2 for l, h in zip(lo, hi))
            span = max(h - l for l, h in zip(lo, hi))
            if span > 0:
                scale = span / 65000
                pos = [[_i16((p[c] - bias[c]) / scale) for p in ps] for c in range(3)]
            else:
                scale = 0.0
                pos = [[0] * frames for _ in range(3)]
        else:
            bias = tuple(j.get("rest_position", (0.0, 0.0, 0.0)))
            scale = 0.0
            pos = [[0] * frames for _ in range(3)]
        tracks.append(Track(crc_of(j["joint"]), rot, pos, bias, scale))
    flags = bytes.fromhex(d.get("flags", "00" * 8))
    return Animation(crc_of(d["name"]), frames, tracks, flags)


def bank_to_dict(bank: AnimBank) -> dict:
    return {"bank": bank.name, "version": bank.version,
            "animations": [animation_to_dict(a) for a in bank.animations]}


def bank_from_dict(d: dict) -> AnimBank:
    """A bank from bank_to_dict's form - or from one animation_to_dict, as a bank of one."""
    if "animations" not in d:
        return AnimBank(d["name"], [animation_from_dict(d)])
    return AnimBank(d["bank"], [animation_from_dict(a) for a in d["animations"]], int(d.get("version", 8)))


def reverse(anim: Animation) -> Animation:
    """The animation played backwards."""
    out = anim.copy()
    for t in out.tracks:
        for ch in t.rot + t.pos:
            ch.reverse()
    return out


# Names recovered by hashing the stock game's strings (Battlefront.exe and every .lvl) and
# the patterns it builds animation names from (human_<weapon>_<posture>_<action>[_alert]):
# 243 of the 320 stock animations and 288 of the 305 joints.
ANIMATION_NAMES = """
_1st_bazooka_flail _1st_bazooka_idle _1st_bazooka_jump _1st_bazooka_jump_land _1st_bazooka_reload
_1st_bazooka_run _1st_bazooka_shoot _1st_droideka_idle _1st_droideka_reload _1st_droideka_shoot
_1st_droideka_walk _1st_grenade_charge _1st_grenade_shoot _1st_grenade_shoot2 _1st_handsdown
_1st_lascannon_flail _1st_lascannon_idle _1st_lascannon_jump _1st_lascannon_run _1st_lascannon_shoot
_1st_rifle_flail _1st_rifle_idle _1st_rifle_jump _1st_rifle_jump_land _1st_rifle_reload
_1st_rifle_run _1st_rifle_shoot _1st_tool_flail _1st_tool_idle _1st_tool_jump _1st_tool_jump_land
_1st_tool_reload _1st_tool_repair _1st_tool_run _1st_tool_shoot activate all_snowspeeder_9pose
death01 fambaa_idle fire fold grab grab1 grab2 human_bazooka_crouch_getdown_prone
human_bazooka_crouch_idle_emote human_bazooka_crouch_reload human_bazooka_crouch_shoot
human_bazooka_crouch_walkbackward human_bazooka_crouch_walkforward human_bazooka_fall
human_bazooka_jump human_bazooka_landhard human_bazooka_landsoft human_bazooka_prone_getup_crouch
human_bazooka_prone_getup_stand human_bazooka_prone_idle_emote human_bazooka_prone_reload
human_bazooka_prone_shoot human_bazooka_stand_getdown_prone human_bazooka_stand_idle_emote
human_bazooka_stand_reload human_bazooka_stand_runbackward human_bazooka_stand_runforward
human_bazooka_stand_shoot human_bazooka_stand_walkbackward human_bazooka_stand_walkforward
human_choking human_drive human_grenade_prone_shoot human_grenade_stand_charge
human_grenade_stand_shoot human_grenade_stand_shoot2 human_grenade_stand_shoot3
human_lascannon_stand_runforward human_pistol_crouch_reload human_pistol_crouch_shoot
human_pistol_prone_reload human_pistol_prone_shoot human_pistol_stand_reload
human_pistol_stand_shoot human_ride human_ride_shoot human_rifle_crouch_getdown_prone
human_rifle_crouch_hitfront human_rifle_crouch_hitleft human_rifle_crouch_hitright
human_rifle_crouch_idle_emote human_rifle_crouch_idle_emote_alert human_rifle_crouch_reload
human_rifle_crouch_shoot human_rifle_crouch_turnleft human_rifle_crouch_turnright
human_rifle_crouch_walkbackward human_rifle_crouch_walkbackward_alert human_rifle_crouch_walkforward
human_rifle_crouch_walkforward_alert human_rifle_diveforward human_rifle_fall
human_rifle_jetpack_hover human_rifle_jump human_rifle_landhard human_rifle_landsoft
human_rifle_prone_getup_crouch human_rifle_prone_getup_stand human_rifle_prone_hitfront
human_rifle_prone_hitleft human_rifle_prone_hitright human_rifle_prone_idle_emote
human_rifle_prone_reload human_rifle_prone_shoot human_rifle_prone_turnleft
human_rifle_prone_turnright human_rifle_prone_walkbackward human_rifle_prone_walkforward
human_rifle_prone_walkleft human_rifle_prone_walkright human_rifle_stand_death_backward
human_rifle_stand_death_forward human_rifle_stand_death_left human_rifle_stand_death_right
human_rifle_stand_getdown_prone human_rifle_stand_getupback human_rifle_stand_getupfront
human_rifle_stand_hitback human_rifle_stand_hitfront human_rifle_stand_hitleft
human_rifle_stand_hitright human_rifle_stand_idle_emote human_rifle_stand_idle_emote_alert
human_rifle_stand_reload human_rifle_stand_runbackward human_rifle_stand_runbackward_alert
human_rifle_stand_runforward human_rifle_stand_runforward_alert human_rifle_stand_shoot
human_rifle_stand_turnleft human_rifle_stand_turnright human_rifle_stand_walkbackward
human_rifle_stand_walkbackward_alert human_rifle_stand_walkforward
human_rifle_stand_walkforward_alert human_rifle_thrown_bouncebacksoft
human_rifle_thrown_bouncefrontsoft human_rifle_thrown_flail human_rifle_thrown_flyingback
human_rifle_thrown_flyingfront human_rifle_thrown_flyingleft human_rifle_thrown_flyingright
human_rifle_thrown_landbacksoft human_rifle_thrown_landfrontsoft human_rifle_thrown_restbacksoft
human_rifle_thrown_restfrontsoft human_rifle_thrown_tumbleback human_rifle_thrown_tumblefront
human_sabre_stand_block_front1 human_sabre_stand_block_front2 human_sabre_stand_block_left1
human_sabre_stand_block_left2 human_sabre_stand_block_rear1 human_sabre_stand_block_rear2
human_sabre_stand_block_right1 human_sabre_stand_block_right2 human_sabre_stand_shoot
human_sabre_stand_shoot3 human_tool_crouch_getdown_prone human_tool_crouch_idle_emote
human_tool_crouch_idle_emote_alert human_tool_crouch_walkbackward
human_tool_crouch_walkbackward_alert human_tool_crouch_walkforward
human_tool_crouch_walkforward_alert human_tool_diveforward human_tool_fall human_tool_jump
human_tool_landhard human_tool_landsoft human_tool_prone_getup_crouch human_tool_prone_getup_stand
human_tool_prone_idle_emote human_tool_prone_walkbackward human_tool_prone_walkforward
human_tool_stand_getdown_prone human_tool_stand_idle_emote human_tool_stand_idle_emote_alert
human_tool_stand_runbackward human_tool_stand_runbackward_alert human_tool_stand_runforward
human_tool_stand_runforward_alert human_tool_stand_walkbackward human_tool_stand_walkbackward_alert
human_tool_stand_walkforward human_tool_stand_walkforward_alert humanlz_drive humanlz_ride
humanlz_rifle_crouch_idle_emote humanlz_rifle_diveforward humanlz_rifle_jetpack_hover
humanlz_rifle_prone_idle_emote humanlz_rifle_stand_death01 humanlz_rifle_stand_deathbackward
humanlz_rifle_stand_deathforward humanlz_rifle_stand_deathleft humanlz_rifle_stand_deathright
humanlz_rifle_stand_flail humanlz_rifle_stand_idle humanlz_rifle_stand_runforward
humanlz_rifle_stand_shoot humanlz_speederbike_9pose idle idle_to_leftfoot idle_to_leftfoot_leftup
idle_to_leftfoot_rightup idle_to_rightfoot imp_speederbike_9pose leftfoot_to_idle
leftfoot_to_idle_leftup leftfoot_to_idle_rightup leftfoot_up rep_fightertank_9pose reset
rightfoot_to_idle rightfoot_to_idle_leftup rightfoot_to_idle_rightup rightfoot_up roll runforward
takeoff tat_skiff_9pose trigger turnl turnl_leftup turnl_rightup turnr turnr_leftup turnr_rightup
unfold walk_leftfoot_rightfoot walk_leftfoot_rightfoot_leftup walk_leftfoot_rightfoot_rightup
walk_rightfoot_leftfoot walk_rightfoot_leftfoot_leftup walk_rightfoot_leftfoot_rightup
""".split()
JOINT_NAMES = """
ALL_Pilot_Helmet aimer_Fgun_1 aimer_Fgun_2 aimer_Fgun_3 aimer_Lgun aimer_Reargun aimer_Rgun
aimer_Rgun_1 aimer_Rgun_2 aimer_fgun_4 aimer_head aimer_leargun aimer_maincannon_x
aimer_maincannon_y aimer_mouthpivot barrel_lcannon barrel_lgun barrel_rcannon barrel_rgun
bone_Rflap1 bone_Rflap2 bone_Rlandinggear1 bone_Rlandinggear2 bone_a_spine bone_b_landingpad
bone_b_leg1 bone_b_leg2 bone_b_spine bone_bl_calf bone_bl_foot bone_bl_footpads bone_bl_lowerarm
bone_bl_pad bone_bl_pelvis bone_bl_thigh bone_bl_toe bone_bl_toe2 bone_bl_toe3 bone_bl_upperarm
bone_bottomflap_R bone_bottomflap_l bone_br_calf bone_br_foot bone_br_footpads bone_br_lowerarm
bone_br_pad bone_br_pelvis bone_br_thigh bone_br_toe bone_br_toe1 bone_br_toe3 bone_br_upperarm
bone_cape1 bone_cape10 bone_cape11 bone_cape12 bone_cape13 bone_cape14 bone_cape15 bone_cape16
bone_cape17 bone_cape18 bone_cape19 bone_cape2 bone_cape20 bone_cape3 bone_cape4 bone_cape5
bone_cape6 bone_cape7 bone_cape8 bone_cape9 bone_cape_a bone_cape_a1 bone_cape_a2 bone_cape_a3
bone_cape_b bone_cape_b1 bone_cape_b2 bone_cape_b3 bone_cape_c bone_cape_c1 bone_cape_c2
bone_cape_c3 bone_cape_d bone_cape_d1 bone_cape_d2 bone_cape_d3 bone_cape_e bone_cape_e1
bone_cape_e2 bone_cape_e3 bone_casing bone_chest bone_claw1 bone_claw2 bone_claw3 bone_claw4
bone_f_geardoor1 bone_f_geardoor2 bone_f_landingpad bone_fflap1 bone_fflap2 bone_fl_calf
bone_fl_foot bone_fl_footpads bone_fl_lowerarm bone_fl_pad bone_fl_pelvis bone_fl_thigh bone_fl_toe
bone_fl_toe2 bone_fl_toe4 bone_fl_upperarm bone_flandinggear1 bone_flandinggear2 bone_flandinggear3
bone_fr_calf bone_fr_foot bone_fr_footpads bone_fr_lowerarm bone_fr_pad bone_fr_pelvis bone_fr_thigh
bone_fr_toe bone_fr_toe2 bone_fr_toe4 bone_fr_upperarm bone_gear1 bone_gear2 bone_gear3
bone_gear_f_1 bone_gear_f_2 bone_gear_f_3 bone_head bone_l_arm1 bone_l_arm2 bone_l_ball bone_l_calf
bone_l_clavicle bone_l_flap bone_l_foil bone_l_foot bone_l_footpedal bone_l_forearm bone_l_gear1
bone_l_gear2 bone_l_gear3 bone_l_gear4 bone_l_gear5 bone_l_gear6 bone_l_gear8 bone_l_gun bone_l_hand
bone_l_handlebar bone_l_landinggear1 bone_l_landinggear2 bone_l_leg1 bone_l_leg2 bone_l_legjoint
bone_l_pad bone_l_rearflap bone_l_shock1 bone_l_shock2 bone_l_shoulder1 bone_l_shoulder2
bone_l_thigh bone_l_toe bone_l_toetip bone_l_tree bone_l_upperarm bone_l_wing bone_l_wrist
bone_leverL bone_leverr bone_lflap1 bone_lflap2 bone_ml_calf bone_ml_footpads bone_ml_pad
bone_ml_toe bone_mount bone_mr_calf bone_mr_footpads bone_mr_pad bone_mr_toe bone_neck bone_neck1
bone_neck2 bone_pelvis bone_r_arm1 bone_r_arm2 bone_r_ball bone_r_calf bone_r_clavicle bone_r_flap
bone_r_foil bone_r_foot bone_r_footpedal bone_r_forearm bone_r_gear1 bone_r_gear2 bone_r_gear3
bone_r_gear4 bone_r_gear5 bone_r_gear6 bone_r_gear8 bone_r_gun bone_r_hand bone_r_handlebar
bone_r_landinggear1 bone_r_landinggear2 bone_r_leg1 bone_r_leg2 bone_r_legjoint bone_r_pad
bone_r_rearflap bone_r_shock1 bone_r_shock2 bone_r_shoulder1 bone_r_shoulder2 bone_r_thigh
bone_r_toe bone_r_toetip bone_r_tree bone_r_upperarm bone_r_wing bone_r_wrist bone_reigns
bone_ribcage bone_rider bone_rlandinggear3 bone_root bone_rudderL bone_rudderr bone_shoulders
bone_spine1 bone_spine2 bone_spine3 bone_tail1 bone_tail2 bone_tail3 bone_tail4 bone_tank01
bone_tank02 bone_tankski_L bone_tankski_R bone_topflap_R bone_topflap_l bone_winglet_l
bone_winglet_r center_box dummyroot hp_active hp_camera hp_cannon_1 hp_cannon_2 hp_fire1 hp_fire2
hp_gun_1 hp_gun_2 hp_weapons sarlacc_bone1 sarlacc_bone2 sarlacc_bone3 sarlacc_bone4 sarlacc_bone5
sarlacc_bone6 sarlacc_bone7 sarlacc_bone8 sarlacc_bone9 sarlacc_rootbone xwing_b_gear1 xwing_b_gear2
xwing_b_gear3 xwing_bl_hatch1 xwing_bl_hatch2 xwing_bl_hatch3 xwing_bl_hatch4 xwing_br_hatch1
xwing_br_hatch2 xwing_br_hatch3 xwing_br_hatch4 xwing_f_gear1 xwing_f_gear2 xwing_f_gear3
xwing_f_hatch1 xwing_f_hatch2
""".split()
anim_learn_names(ANIMATION_NAMES)
anim_learn_names(JOINT_NAMES)


# ==================================================================================================
# bf1_config - merged in from bf1_config.py
# ==================================================================================================
# bf1_config.py - read and write the game's munged config chunks, the
# compiled form of the mod tools' text config files:
#
#   fx__  .fx    effects: water, fog, godrays, colour control, precipitation, ...
#   sky_  .sky   sky dome, sun, fog and ambient light
#   bnd_  .bnd   map boundaries
#   prp_  .prp   foliage and props scattered over the terrain
#   load  .cfg   loading screens
#   snd_  .snd   sound properties (core.lvl)
#   mcfg  .mcfg  music configuration (shell.lvl)
#
# All are a NAME block (the hash of the file name) followed by a tree:
#
#   DATA  one line, `Name(value, value, ...)`:
#           u32  hash of the name (bf1_core.fnv1a32)
#           u8   value count
#           u32  per value: a float, a hash, or - for a string - its offset
#                (from the first value; the characters are 4 bytes on)
#           u32  size of the strings, then the strings, each ending in a NUL
#   SCOP  a `{ ... }` block belonging to the DATA before it
#
# to_text() writes that tree in the original file syntax, e.g.
#
#   Effect("Godray")
#   {
#       Enable(1);
#       MaxGodraysInWorld(1000);
#   }
#
# and from_text() reads it back. Values are written as numbers, "strings" or
# 0x12345678 (a hash or a raw value); a hash whose text is known is written
# hash("name"). Names the tools don't know show as 0x... and work as written.



CONFIG_TAGS = {"fx__": ".fx", "sky_": ".sky", "bnd_": ".bnd", "prp_": ".prp", "load": ".cfg", "snd_": ".snd",
               "mcfg": ".mcfg"}


class ConfigError(Exception):
    pass


# --- names ---------------------------------------------------------------------------------

_config_names: dict = {}


def config_learn_names(names) -> None:
    for n in names:
        n = n.strip()
        if n:
            _config_names.setdefault(fnv1a32(n), n)


def config_name_of(h: int):
    return _config_names.get(h) or HASH_TO_NAME.get(h)


# --- the tree --------------------------------------------------------------------------------

class ConfigLine:
    """One DATA entry and, if it opens a { } block, the lines inside."""
    __slots__ = ("hash", "values", "children")

    def __init__(self, h, values, children=None):
        self.hash, self.values, self.children = h, values, children


class ConfigValue:
    """kind: 'num' (float32 bits), 'str' (bytes without the NUL), 'raw' (u32 shown as hex)."""
    __slots__ = ("kind", "v")

    def __init__(self, kind, v):
        self.kind, self.v = kind, v

    def __eq__(self, other):
        return isinstance(other, ConfigValue) and (self.kind, self.v) == (other.kind, other.v)


def _config_blocks(data: bytes):
    pos = 0
    while pos + 8 <= len(data):
        tag = data[pos:pos + 4]
        size = struct.unpack_from("<I", data, pos + 4)[0]
        yield tag, data[pos + 8:pos + 8 + size]
        pos += 8 + size + (-size % 4)


def _config_looks_like_float(bits: int) -> bool:
    f = struct.unpack("<f", struct.pack("<I", bits))[0]
    if bits in (0, 0x80000000):
        return True
    if not math.isfinite(f):
        return False
    return 1e-6 <= abs(f) <= 1e9


def _config_decode_data(body: bytes) -> tuple:
    h, n = struct.unpack_from("<IB", body, 0)
    words = struct.unpack_from(f"<{n}I", body, 5)
    rest = body[5 + 4 * n:]
    size = struct.unpack_from("<I", rest, 0)[0] if len(rest) >= 4 else 0
    strings = rest[4:4 + size]
    values = []
    for w in words:
        if size and 4 * n <= w < 4 * n + size:
            start = w - 4 * n
            end = strings.index(b"\0", start)
            values.append(ConfigValue("str", strings[start:end]))
        elif _config_looks_like_float(w):
            values.append(ConfigValue("num", w))
        else:
            values.append(ConfigValue("raw", w))
    return h, values


def config_decode(payload: bytes) -> tuple:
    """(name hash, [Line])"""
    name_hash = None
    top = []
    stack = [top]

    def walk(data, into):
        for tag, body in _config_blocks(data):
            if tag == b"NAME":
                nonlocal name_hash
                name_hash = struct.unpack_from("<I", body, 0)[0] if len(body) >= 4 else 0
            elif tag == b"DATA":
                h, values = _config_decode_data(body)
                into.append(ConfigLine(h, values))
            elif tag == b"SCOP":
                if not into:
                    raise ConfigError("A { } block with no line before it.")
                into[-1].children = []
                walk(body, into[-1].children)
            else:
                raise ConfigError(f"Unexpected block '{tag.decode('latin-1')}' in a config chunk.")

    walk(payload, top)
    del stack
    return name_hash, top


def _config_encode_data(line: ConfigLine) -> bytes:
    n = len(line.values)
    if n > 255:
        raise ConfigError("A line can't have more than 255 values.")
    words, strings = [], bytearray()
    for v in line.values:
        if v.kind == "str":
            words.append(4 * n + len(strings))
            strings += v.v + b"\0"
        else:
            words.append(v.v & 0xFFFFFFFF)
    return struct.pack(f"<IB{n}I", line.hash, n, *words) + struct.pack("<I", len(strings)) + bytes(strings)


def _config_block(tag: bytes, body: bytes) -> bytes:
    return tag + struct.pack("<I", len(body)) + body + bytes(-len(body) % 4)


def config_encode(name_hash: int, lines: list) -> bytes:
    def lines_bytes(ls):
        out = bytearray()
        for line in ls:
            out += _config_block(b"DATA", _config_encode_data(line))
            if line.children is not None:
                out += _config_block(b"SCOP", lines_bytes(line.children))
        return bytes(out)
    return _config_block(b"NAME", struct.pack("<I", name_hash)) + lines_bytes(lines)


# --- text ------------------------------------------------------------------------------------

def _config_fmt_float(bits: int) -> str:
    f = struct.unpack("<f", struct.pack("<I", bits))[0]
    if bits == 0x80000000:
        return "-0.0"
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    for digits in range(1, 12):
        s = f"{f:.{digits}g}"
        if struct.unpack("<I", struct.pack("<f", float(s)))[0] == bits:
            return s
    return repr(f)


def _config_quote(b: bytes) -> str:
    s = b.decode("latin-1")
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _config_fmt_value(v: ConfigValue) -> str:
    if v.kind == "num":
        return _config_fmt_float(v.v)
    if v.kind == "str":
        return _config_quote(v.v)
    known = config_name_of(v.v)
    if known and fnv1a32(known) == v.v:
        return f'hash("{known}")'
    return f"0x{v.v:08x}"


def _config_fmt_name(h: int) -> str:
    known = config_name_of(h)
    return known if known and fnv1a32(known) == h else f"0x{h:08x}"


def config_to_text(lines: list, indent: str = "\t") -> str:
    out = []

    def emit(ls, depth):
        for line in ls:
            pad = indent * depth
            head = f"{pad}{_config_fmt_name(line.hash)}({', '.join(_config_fmt_value(v) for v in line.values)})"
            if line.children is None:
                out.append(head + ";")
            else:
                out.append(head)
                out.append(pad + "{")
                emit(line.children, depth + 1)
                out.append(pad + "}")
                if depth == 0:
                    out.append("")

    emit(lines, 0)
    return "\n".join(out).rstrip("\n") + "\n"


_CONFIG_TOKEN = re.compile(r'\s+|//[^\n]*|--[^\n]*|"(?:\\.|[^"\\])*"|0[xX][0-9a-fA-F]+|'
                    r'[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?|[A-Za-z_][A-Za-z0-9_.]*|[(){},;]')


def config_from_text(text: str) -> list:
    """[Line] from to_text's syntax (also the original config file syntax)."""
    tokens = []
    pos, line_no = 0, 1
    while pos < len(text):
        m = _CONFIG_TOKEN.match(text, pos)
        if not m:
            raise ConfigError(f"Line {line_no}: can't read {text[pos:pos + 12]!r}.")
        tok = m.group(0)
        if not (tok[0].isspace() or tok.startswith("//") or tok.startswith("--")):
            tokens.append((tok, line_no))
        line_no += tok.count("\n")
        pos = m.end()
    i = 0

    def peek():
        return tokens[i][0] if i < len(tokens) else None

    def take(expected=None):
        nonlocal i
        if i >= len(tokens):
            raise ConfigError(f"The text ends early{f' - expected {expected}' if expected else ''}.")
        tok, ln = tokens[i]
        if expected and tok != expected:
            raise ConfigError(f"Line {ln}: expected '{expected}', found '{tok}'.")
        i += 1
        return tok, ln

    def name_hash(tok, ln):
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        if not re.match(r"[A-Za-z_]", tok):
            raise ConfigError(f"Line {ln}: '{tok}' isn't a name.")
        config_learn_names([tok])
        return fnv1a32(tok)

    def value():
        tok, ln = take()
        if tok.startswith('"'):
            body = tok[1:-1]
            return ConfigValue("str", re.sub(r"\\(.)", r"\1", body).encode("latin-1"))
        if tok.lower().startswith("0x"):
            return ConfigValue("raw", int(tok, 16) & 0xFFFFFFFF)
        if tok == "hash":
            take("(")
            s, ln2 = take()
            if not s.startswith('"'):
                raise ConfigError(f"Line {ln2}: hash() takes a \"string\".")
            take(")")
            text = s[1:-1]
            config_learn_names([text])
            return ConfigValue("raw", fnv1a32(text))
        try:
            f = float(tok)
        except ValueError:
            raise ConfigError(f"Line {ln}: '{tok}' isn't a value (a number, \"string\" or 0x...).") from None
        return ConfigValue("num", struct.unpack("<I", struct.pack("<f", f))[0])

    def lines(closing):
        out = []
        while peek() is not None and peek() != closing:
            tok, ln = take()
            h = name_hash(tok, ln)
            take("(")
            values = []
            if peek() != ")":
                values.append(value())
                while peek() == ",":
                    take(",")
                    values.append(value())
            take(")")
            line = ConfigLine(h, values)
            if peek() == "{":
                take("{")
                line.children = lines("}")
                take("}")
            elif peek() == ";":
                take(";")
            out.append(line)
        return out

    result = lines(None)
    if i < len(tokens):
        raise ConfigError(f"Line {tokens[i][1]}: unexpected '{tokens[i][0]}'.")
    return result


def config_decode_text(payload: bytes) -> str:
    _name, lines = config_decode(payload)
    return config_to_text(lines)


def config_encode_text(payload: bytes, text: str) -> bytes:
    """A new payload: `payload`'s NAME with the lines from `text`."""
    name_hash, _lines = config_decode(payload)
    return config_encode(name_hash or 0, config_from_text(text))


# Names recovered by hashing the game's own strings and combinations of the words they're
# made of (FogColor, SkyInfo, WaterRingColor, ...): 248 of the 376 the stock configs use.
CONFIG_NAMES = """
Acceleration Alpha AlphaMin AlphaMinMax Ambient ambientcolor AmbientScale Angle AnimationFrequency
attenuatebus Attenuation attenuationleadin AttenuationLeadOut BackAngle BackColor Backdrop Bias
BlendMode BlockSize Blue BlurRes BlurValue BottomColor BottomDirectionalAmbientColor BottomRange
Boundary BrightnessMin BumpMap Bus Centered CharacterAmbientColor CloudLayer CollisionSound Color
ColorSaturation ColorVariation ConstantBlend Count DecayHFRatio Decaytime Density DetailTexture
DetailTextureScale Diffusion DirectGain Distance DomeColor DomeInfo DomeModel downsizefactor
DustVelocity Effect Enable Enabled Erdelay ERGain FadeDist FadeInTime FadeOutTime FadeTime
FadeViewDistance FarSceneRange File Filter Flip FoamTexture FoamTile FogColor FogFar FogNear
FogRange FrameSize Frequency FresnelMinMax Gain GainDev gammabrightness GammaColorBalance
gammacontrast Gammahue Geometry GeometryHeight Grasspatch Green GroundEffect Height HFReference Hue
InDirectionFactor Intensity Layer Leafpatch LifeSpan LifeTime LightningBolt Loaddisplay LogoTexture
Looping MainTexture MaxDistance MaxLength MaxSize MaxViewDistance Mesh MinDistance MinSize Mode
Mode3d ModeDoppler Model Move MoveX MoveY MoveZ Movie MovieProperties MuteDistance Name
NearSceneRange Next NumStars OceanEnable ODF Offset OffsetAngle OffTime On OnTime Pan Parent
ParticleDensity ParticleDensityRange particleemitter ParticleSize PatchDivisions PatchResolution
Path Pitch PitchDev PlanetInfo PlanetLevel PlayInterval playintervaldev playprobability Position
PositionScale PositionX PositionY PositionZ Priority Radius RainInfo Randomseed Range Red
reflectioncolor ReflectionFogColor RefractionColor ReverbDelay ReverbGain Right Rollin
RollInDistance RollOff RoomGain RoomHFGain RoomRollOff Rotation RotationRange RotationVelocity
Sample Samplelist Saturation Scale ScrollRate ScrollSpeed Segment SegmentList ShadowColor Shield
Size SkyInfo SkyObject SoundName soundproperties Space Spawner SpecularColor Spread SpreadFactor
Stars StarsInfo Startdelay StartRotation Startx Starty StartZ StreakLength StringAngle SunColor
sundirection SunFlare SunInfo SunlightFadeFactor TargetTime TeamModel TerrainBumpTexture
TerrainEnable Texture TextureScale TextureSize TextureSpeed TextureVelocity Threshold Tile TileSize
TopColor TopDirectionalAmbientColor TopRange Transformer TreeLine TurnOffTime TurnOnTime Type
TypeCount UnderwaterColor Up Value VehicleAmbientColor Velocity VelocityChangeRate VelocityRange
VelocityScale VelocityY VelocityZ ViewAngleFactor WaterInfo WaterRingColor WaterSplashColor
WaterWakeColor Width WindDirection WindSpeed World WorldBrightness WorldContrast WorldHeight
WorldSaturation
""".split()
config_learn_names(CONFIG_NAMES)
config_learn_names(["Effect", "ColorControl", "Godray", "FogCloud", "Blur", "Water", "Precipitation", "Wind", "Lightning",
             "Shadow", "MotionBlur", "ScopeBlur", "HDR", "SpecularLighting", "Sun", "Rain", "Snow", "Dust"])

# The NAME block is the hash of the source file's name (bes1.fx -> "bes1"); these are the
# ones recovered from the game's strings: 86 of the 153 stock configs.
CONFIG_FILE_NAMES = {fnv1a32(n): n for n in """
arccaster_lightning atat_explosion bes1 bespin2 bigdirtspray bigwalkerstomp bloodspray
bunker_lightning chargeup chunksmoke com_sfx_weap_grenade_blue com_sfx_weap_grenade_gold
com_sfx_weap_grenade_green com_sfx_weap_grenade_red com_sfx_weap_grenade_thermal dirtspray
droidekashield dustwake end1 exhaust_cloudcar exhaust_jedistarfighter exhaust_kwing exhaust_xwing
explosion fambaa_shield flyerspray geo1 geo_blunderbuss_blast global hailfire_wake headshot hoth
jetpack kamino1 kas2 kashyyyk1 load lrg_weap_discharge med_explosion mediumsmoketrail
mortartrail_red naboo1 naboo2 ord_explosion powerup_blue powerup_red protontorpedo rainsplash
ren1_dryice rhenvar1 rhenvar2 sbd_missiles_red small_explosion smokeplume smoketrail smoketrail_blue
smoketrail_gold smoketrail_green smoketrail_purple spark spiderdroid_ord_exp super_explosion tat1
tat2 tinysmoketrail vehicleflame vehiclesmoke vehiclespark walkerstomp waterbulletspray
waterfall_bottom_center waterfall_bottom_side waterfountain watersplash_lg watersplash_md
watersplash_ord watersplash_ord_lg watersplash_ord_md watersplash_sm watersplash_wade
watersplash_walker waterspout waterwake_lg waterwake_sm yav2 yavin1
""".split()}


def config_file_name(payload: bytes) -> str:
    """A config chunk's name - its source file's name when known, else its hash."""
    if len(payload) < 12 or payload[:4] != b"NAME":
        return ""
    h = struct.unpack_from("<I", payload, 8)[0]
    return CONFIG_FILE_NAMES.get(h) or f"0x{h:08x}"


# ==================================================================================================
# bf1_locl - merged in from bf1_locl.py
# ==================================================================================================
# bf1_locl.py - read and write the game's text: 'Locl' chunks, one per
# language, all in core.lvl (english, spanish, italian, french, german,
# uk_english). Every name, menu, hint and message the game shows is here.
#
#   Locl  NAME  the language
#         BODY  one entry per string:
#                 u32  hash of the key (bf1_core.fnv1a32, e.g. "ifs.main.exit")
#                 u16  entry size in bytes, header included
#                      (6 + the text + 2 zero characters, rounded up to 4;
#                      an empty string takes 16)
#                 the text, UTF-16LE, then zero padding
#               then a run of zero bytes ends the list
#
# Keys are only stored as hashes. KEY_NAMES holds the ones recovered from the
# game's own strings (scripts reference keys like "ifs.controls.crouch"); the
# rest show as 0x... hashes, which work anywhere a key name does.



LOCL_LANGUAGES = ("english", "uk_english", "french", "german", "italian", "spanish")


class LoclError(Exception):
    pass


_locl_names: dict = {}


def learn_keys(names) -> None:
    for n in names:
        n = n.strip()
        if n:
            _locl_names.setdefault(fnv1a32(n), n)


def key_name(h: int) -> str:
    return _locl_names.get(h) or f"0x{h:08x}"


def key_hash(key: str) -> int:
    """A key name, or a hash written as 0x... (how unnamed keys are shown)."""
    key = key.strip()
    if key.lower().startswith("0x"):
        return int(key, 16)
    learn_keys([key])
    return fnv1a32(key)


@dataclass
class LoclStrings:
    language: str
    entries: list = field(default_factory=list)   # [hash, text], in file order
    tail: bytes = bytes(7)                          # the zero bytes after the last entry
    raw: dict = field(default_factory=dict)         # (hash, text) -> the entry's original bytes

    def get(self, key: str):
        h = key_hash(key)
        for e in self.entries:
            if e[0] == h:
                return e[1]
        return None

    def set(self, key: str, text: str, occurrence: int | None = None) -> str | None:
        """Sets a key's text, adding the key if it's new, and returns the old
        text. A few keys appear more than once; `occurrence` picks one
        (0 = the first), otherwise every copy is set."""
        h = key_hash(key)
        old, n = None, 0
        for e in self.entries:
            if e[0] == h:
                if occurrence is None or n == occurrence:
                    if old is None:
                        old = e[1]
                    e[1] = text
                n += 1
        if old is None:
            self.entries.append([h, text])
        return old

    def remove(self, key: str) -> bool:
        h = key_hash(key)
        before = len(self.entries)
        self.entries = [e for e in self.entries if e[0] != h]
        return len(self.entries) != before


def decode_locl(payload: bytes) -> LoclStrings:
    body = parse_body(payload)
    name = _find_child(body, "NAME")
    data = _find_child(body, "BODY")
    if data is None:
        raise LoclError("No BODY block - not a text chunk.")
    b = data.payload
    out = LoclStrings(decode_ascii(name.payload) if name else "")
    pos = 0
    while pos + 6 <= len(b):
        h, size = struct.unpack_from("<IH", b, pos)
        if size == 0:
            break
        if size < 6 or pos + size > len(b):
            raise LoclError(f"Bad entry at byte {pos}.")
        text = b[pos + 6:pos + size].decode("utf-16-le", "replace").split("\0", 1)[0]
        out.entries.append([h, text])
        out.raw[(h, text)] = b[pos:pos + size]
        pos += size
    out.tail = b[pos:]
    return out


def _locl_entry(h: int, text: str) -> bytes:
    raw = text.encode("utf-16-le") + b"\0\0\0\0"
    size = (6 + len(raw) + 3) // 4 * 4 if text else 16  # the game's empty strings take 16
    if size > 0xFFFF:
        raise LoclError(f"{key_name(h)}: text is too long ({len(text)} characters).")
    return struct.pack("<IH", h, size) + raw.ljust(size - 6, b"\0")


def encode_locl(strings: LoclStrings) -> bytes:
    # unchanged entries keep their exact bytes - the game's own padding isn't
    # always the minimum, and there's no reason to disturb it
    body = b"".join(strings.raw.get((h, t)) or _locl_entry(h, t) for h, t in strings.entries) + (strings.tail or bytes(7))
    return (tagged_block(b"NAME", strings.language.encode("ascii") + b"\0")
            + tagged_block(b"BODY", body))


# --- text file form ------------------------------------------------------------------------
# One string per line: key, a tab, the text. Line breaks and tabs inside a
# string are written as \n and \t, and a backslash as \\.

def _locl_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")


def _locl_unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append({"n": "\n", "r": "\r", "t": "\t", "\\": "\\"}.get(nxt, "\\" + nxt))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def locl_to_text(strings: LoclStrings, entries=None) -> str:
    rows = strings.entries if entries is None else entries
    return "".join(f"{key_name(h)}\t{_locl_escape(t)}\n" for h, t in rows)


def locl_from_text(text: str) -> list:
    """[(hash, text)] from to_text's form; blank lines and lines starting with # are skipped."""
    rows = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("#"):
            continue
        key, tab, value = line.partition("\t")
        if not tab:
            raise LoclError(f"Line {n} has no tab between the key and the text.")
        rows.append((key_hash(key), _locl_unescape(value)))
    return rows


# Key names recovered by hashing the game's own strings (Battlefront.exe, scripts and
# levels) and the patterns they follow (entity.<side>.<class>, level.<map>.objectives.<n>, ...):
# 1101 of the 2,446 stock keys.
KEY_NAMES = """
common.accept common.always common.back common.cancel common.change common.continue common.era.cw
common.era.gcw common.join common.meta.active common.mp.connect common.mp.creating_gsid
common.mp.dedicated common.mp.downloadable common.mp.fastclient common.mp.gamename common.mp.joining
common.mp.joinip common.mp.joinip_prompt common.mp.launch common.mp.leaving common.mp.loggingin_gsid
common.mp.options common.mp.signout common.mp.stats common.mp.title common.never common.no
common.none common.off common.ok common.on common.pctspcts common.quit common.quit2windows
common.reset common.restart common.resume common.select common.sides.all.hero
common.sides.all.longname common.sides.all.name common.sides.cis.hero common.sides.cis.longname
common.sides.cis.name common.sides.imp.hero common.sides.imp.longname common.sides.imp.name
common.sides.rep.hero common.sides.rep.longname common.sides.rep.name common.vehiclepositions.bomber
common.vehiclepositions.copilot common.vehiclepositions.gunner common.vehiclepositions.leftturret
common.vehiclepositions.passenger common.vehiclepositions.pilot common.vehiclepositions.rightturret
common.yes entity.all.droid_r2d2 entity.all.fly_kwing entity.all.fly_snowspeeder
entity.all.fly_xwing entity.all.fly_ywing entity.all.hover_combatspeeder entity.all.hover_hovernaut
entity.all.inf_lukeskywalker entity.all.inf_lukeskywalkersnow entity.all.inf_marksman
entity.all.inf_marksmanjungle entity.all.inf_marksmansnow entity.all.inf_pilot
entity.all.inf_smuggler entity.all.inf_smugglersnow entity.all.inf_soldier
entity.all.inf_soldierdesert entity.all.inf_soldierjungle entity.all.inf_soldiersnow
entity.all.inf_soldierurban entity.all.inf_vanguard entity.all.inf_vanguardjungle
entity.all.inf_vanguardsnow entity.all.walk_tauntaun entity.all.weap_inf_remotedroid_ord
entity.bes.bldg_aaa_turret entity.bes.fly_cloudcar entity.cis.fly_droidfighter
entity.cis.fly_geofighter entity.cis.fly_maf entity.cis.hover_aat entity.cis.hover_mtt
entity.cis.hover_stap entity.cis.inf_assassindroid entity.cis.inf_assault entity.cis.inf_battledroid
entity.cis.inf_battledroid_av entity.cis.inf_battledroid_inf entity.cis.inf_battledroid_pilot
entity.cis.inf_battledroid_sniper entity.cis.inf_countdooku entity.cis.inf_droideka
entity.cis.inf_pilotdroid entity.cis.tread_hailfire entity.cis.walk_spider
entity.cis.weap_inf_remotedroid_ord entity.com.weap_gunturret entity.des.tat_inf_jawa
entity.des.tat_inf_tuskenhunter entity.ewk.ewk_inf_repair entity.ewk.ewk_inf_scout
entity.ewk.ewk_inf_trooper entity.ewk.inf_repair entity.ewk.inf_scout entity.ewk.inf_trooper
entity.gar.gar_inf_pilot entity.gar.gar_inf_soldier entity.geo.bldg_geoturret
entity.geo.inf_geonosian entity.gun.gun_inf_defender entity.gun.gun_inf_rider
entity.gun.gun_inf_soldier entity.gun.gun_walk_kaadu entity.gun.inf_defender
entity.gun.inf_fusioncutter entity.gun.inf_mine_dispenser entity.gun.inf_rider
entity.gun.inf_soldier entity.gun.walk_kaadu entity.hoth.bldg_dish_turret
entity.hoth.bldg_hoth_turret entity.imp.droid_probe entity.imp.fly_tiebomber
entity.imp.fly_tiefighter entity.imp.fly_trooptrans entity.imp.hover_fightertank
entity.imp.hover_speederbike entity.imp.inf_dark_trooper entity.imp.inf_dark_troopersnow
entity.imp.inf_darthvader entity.imp.inf_pilotatat entity.imp.inf_pilotatst entity.imp.inf_pilottie
entity.imp.inf_scout_trooper entity.imp.inf_scout_troopersnow entity.imp.inf_shock_trooper
entity.imp.inf_shock_troopersnow entity.imp.inf_storm_trooper entity.imp.inf_storm_troopersnow
entity.imp.walk_atat entity.imp.walk_atst entity.imp.walk_atst_jungle entity.imp.walk_atst_snow
entity.imp.weap_inf_remotedroid_ord entity.kam.bldg_aaa_turret entity.nab.hover_gianspeeder
entity.rep.fly_gunship entity.rep.fly_jedifighter entity.rep.hover_combatspeeder
entity.rep.hover_fightertank entity.rep.hover_speederbike entity.rep.inf_arc_trooper
entity.rep.inf_clone_pilot entity.rep.inf_clone_sharpshooter entity.rep.inf_clone_trooper
entity.rep.inf_jet_trooper entity.rep.inf_macewindu entity.rep.walk_atte
entity.rep.weap_inf_remotedroid_ord entity.tat.bldg_barge_turret entity.tat.hover_skiff
entity.tat.inf_jawa entity.tat.inf_tuskenhunter entity.tat.inf_tuskenraider entity.wok.inf_mechanic
entity.wok.inf_rocketeer entity.wok.inf_warrior entity.yav.bldg_tower_turret game.attractmode.title
game.infomsg.bootplayer game.infomsg.captured game.infomsg.died game.infomsg.joined
game.infomsg.killed game.infomsg.left game.infomsg.lost game.infomsg.suicide game.infomsg.teamkill
game.mapdisplay.hints game.mapdisplay.objectives game.pause.followcam game.pause.freecam
game.pause.playerlist game.pause.stats game.pause.suicide game.pause.suicide_prompt game.pause.title
game.spawndisplay.characterdisplay.promptnext game.spawndisplay.characterdisplay.promptprev
game.spawndisplay.spawndisplay.promptnext game.spawndisplay.spawndisplay.promptprev
game.spawndisplay.spawnpointtitle game.spawndisplay.vehiclelist game.tooltips.after_enter_turret
game.tooltips.after_enter_vehicle game.tooltips.approach_cp game.tooltips.approach_health_ammo
game.tooltips.approach_turret game.tooltips.approach_unbuilt game.tooltips.approach_unbuilt_ammo
game.tooltips.approach_unbuilt_health game.tooltips.approach_vehicle
game.tooltips.controls.changeprimary game.tooltips.controls.changesecondary
game.tooltips.controls.jump game.tooltips.controls.prone game.tooltips.disablemsg
game.tooltips.flyertakeoff game.tooltips.leaving_battlefield game.tooltips.reinforcements
game.tooltips.select_soldier game.tooltips.select_spawn_location gamespy.status.gp_away
gamespy.status.gp_chatting gamespy.status.gp_offline gamespy.status.gp_online
gamespy.status.gp_playing gamespy.status.gp_staging gamespy.status.unknown ifs.busytime
ifs.common.convert_profile ifs.controls.backspace ifs.controls.btn1 ifs.controls.btn2
ifs.controls.btn3 ifs.controls.btn4 ifs.controls.camera ifs.controls.chat ifs.controls.commchat0
ifs.controls.commchat1 ifs.controls.commchat2 ifs.controls.commchat3 ifs.controls.commchat4
ifs.controls.commchat5 ifs.controls.commchat6 ifs.controls.commchat7 ifs.controls.crouch
ifs.controls.delete ifs.controls.flyer.title ifs.controls.flyertab ifs.controls.general.action
ifs.controls.general.crouch ifs.controls.general.enter ifs.controls.general.exit
ifs.controls.general.firstthirdperson ifs.controls.general.freelook ifs.controls.general.jump
ifs.controls.general.land_takeoff ifs.controls.general.look_strafe ifs.controls.general.map
ifs.controls.general.move_strafe ifs.controls.general.move_turn ifs.controls.general.nextposition
ifs.controls.general.none ifs.controls.general.pause ifs.controls.general.pitch_only
ifs.controls.general.pitch_strafe ifs.controls.general.pitch_turn ifs.controls.general.primary
ifs.controls.general.primarynext ifs.controls.general.primaryprev ifs.controls.general.reload
ifs.controls.general.rollleft ifs.controls.general.rollright ifs.controls.general.secondary
ifs.controls.general.secondarynext ifs.controls.general.secondaryprev
ifs.controls.general.squadcommands ifs.controls.general.squaddown ifs.controls.general.squadleft
ifs.controls.general.squadright ifs.controls.general.squadup ifs.controls.general.throttle_only
ifs.controls.general.throttle_roll ifs.controls.general.throttle_strafe
ifs.controls.general.throttle_turn ifs.controls.general.zoom ifs.controls.joyaxislx
ifs.controls.joyaxisly ifs.controls.joyaxisrx ifs.controls.joyaxisry ifs.controls.jump
ifs.controls.leftstick ifs.controls.lefttrig1 ifs.controls.lefttrig2 ifs.controls.lookdown
ifs.controls.lookleft ifs.controls.looklraxis ifs.controls.lookright ifs.controls.lookudaxis
ifs.controls.lookup ifs.controls.lraxis ifs.controls.map ifs.controls.mbtn0 ifs.controls.mbtn1
ifs.controls.mbtn2 ifs.controls.mbtn3 ifs.controls.mbtn4 ifs.controls.mbtn5 ifs.controls.mbtn6
ifs.controls.mbtn7 ifs.controls.mousex ifs.controls.mousey ifs.controls.moveback
ifs.controls.moveforward ifs.controls.moveleft ifs.controls.moveright ifs.controls.mwheeldn
ifs.controls.mwheelup ifs.controls.paddown ifs.controls.padleft ifs.controls.padright
ifs.controls.padup ifs.controls.pause ifs.controls.pitchaxis ifs.controls.pitchdwn
ifs.controls.pitchup ifs.controls.playerlist ifs.controls.primaryfire ifs.controls.primarynext
ifs.controls.primaryprev ifs.controls.prone ifs.controls.reload ifs.controls.reset
ifs.controls.rightstick ifs.controls.righttrig1 ifs.controls.righttrig2 ifs.controls.secondaryfire
ifs.controls.secondarynext ifs.controls.secondaryprev ifs.controls.select ifs.controls.soldier.title
ifs.controls.soldiertab ifs.controls.squaddown ifs.controls.squadleft ifs.controls.squadright
ifs.controls.squadup ifs.controls.start ifs.controls.strafeaxis ifs.controls.strafeleft
ifs.controls.straferight ifs.controls.takescreenshot ifs.controls.teamchat ifs.controls.throttleaxis
ifs.controls.throttledec ifs.controls.throttleinc ifs.controls.title ifs.controls.turnaxis
ifs.controls.turnleft ifs.controls.turnright ifs.controls.udaxis ifs.controls.vehicle.title
ifs.controls.vehicle_unit ifs.controls.vehicletab ifs.controls.view ifs.controls.zoom
ifs.credits.additionalart ifs.credits.additionalcode ifs.credits.additionaldes
ifs.credits.additionalprog ifs.credits.addsounddesign ifs.credits.artists
ifs.credits.assistantleadtesters ifs.credits.assistantproducer ifs.credits.associateproducer
ifs.credits.asstvoiceeditor ifs.credits.bink ifs.credits.buildengineer ifs.credits.burninggoddesses
ifs.credits.cast ifs.credits.ceo ifs.credits.chriscox ifs.credits.compatibilitylead
ifs.credits.compatibilitysupervisor ifs.credits.compatibilitytechnicians ifs.credits.compliance
ifs.credits.compliancelead ifs.credits.contentcoordinator ifs.credits.davidcollins
ifs.credits.davidrobb ifs.credits.designconsultant ifs.credits.designers ifs.credits.director
ifs.credits.directorofhr ifs.credits.directorops ifs.credits.enzymelabs ifs.credits.executiveartdir
ifs.credits.executiveproducer ifs.credits.expat ifs.credits.expat2 ifs.credits.externalartdirector
ifs.credits.foleyartist ifs.credits.foleyassist ifs.credits.foleyrecordedat
ifs.credits.foleyrecordist ifs.credits.gamespy ifs.credits.gwchilds ifs.credits.interfacedesign
ifs.credits.internationalproducers ifs.credits.internationalqa ifs.credits.internationalqalead
ifs.credits.internetmarketing ifs.credits.interns ifs.credits.launcherinstallprog
ifs.credits.leadartists ifs.credits.leaddesigner ifs.credits.leadprogrammers ifs.credits.leadtester
ifs.credits.leadtesters ifs.credits.leadvoiceeditor ifs.credits.libpng ifs.credits.lucaslicensing
ifs.credits.manualwriting ifs.credits.masterlab ifs.credits.musicediting ifs.credits.nickjamison
ifs.credits.origstarwarssfx ifs.credits.origswmusic ifs.credits.origswmusiclegal
ifs.credits.president ifs.credits.prodmarkmanager ifs.credits.prodsupport
ifs.credits.prodsupportsupervisor ifs.credits.producer ifs.credits.producers ifs.credits.production
ifs.credits.programmers ifs.credits.publicrelations ifs.credits.qaspecialthanks
ifs.credits.qasupervisor ifs.credits.qatesters ifs.credits.qualityman ifs.credits.qualityservices
ifs.credits.saleschannelmarketing ifs.credits.seniormarkcoordinator ifs.credits.sounddesign
ifs.credits.specialthanks ifs.credits.supervisingproducer ifs.credits.temuramorrison
ifs.credits.title ifs.credits.tomkane ifs.credits.veryspecialthanks ifs.credits.vodirector
ifs.credits.voiceintlcoordinator ifs.credits.voiceintldeptmanager ifs.credits.zlib
ifs.difficulty.easy ifs.difficulty.hard ifs.difficulty.medium ifs.difficulty.title
ifs.error.no_net_device ifs.gameopt.autoaim ifs.gameopt.cam_cockpit ifs.gameopt.cam_forward
ifs.gameopt.camera ifs.gameopt.diff ifs.gameopt.difficulty ifs.gameopt.friendlyfire ifs.gameopt.hero
ifs.gameopt.pc_configbindings ifs.gameopt.pc_gamepad ifs.gameopt.pc_invert ifs.gameopt.pc_joysens
ifs.gameopt.pc_keybindpopup ifs.gameopt.pc_keyconfigtitle ifs.gameopt.pc_mousesens
ifs.gameopt.pc_optionstitle ifs.gameopt.rumble ifs.gameopt.subtitles ifs.gameopt.title
ifs.gameopt.tt_auto ifs.gameopt.tt_off ifs.gameopt.tt_on ifs.gsprofile.all ifs.gsprofile.askcreate
ifs.gsprofile.autologin ifs.gsprofile.badchars ifs.gsprofile.badchars2 ifs.gsprofile.bademail
ifs.gsprofile.create ifs.gsprofile.email ifs.gsprofile.howtomanage ifs.gsprofile.login
ifs.gsprofile.mustspecify ifs.gsprofile.nick ifs.gsprofile.nologin ifs.gsprofile.none
ifs.gsprofile.nopassword ifs.gsprofile.password ifs.gsprofile.prompt ifs.gsprofile.saveinprofile
ifs.gsprofile.title ifs.gsprofile.warn_data ifs.loadsave_pc.cancel ifs.loadsave_pc.continue
ifs.loadsave_pc.no ifs.loadsave_pc.ok ifs.loadsave_pc.save02 ifs.loadsave_pc.save03
ifs.loadsave_pc.save04 ifs.loadsave_pc.save05 ifs.loadsave_pc.save06 ifs.loadsave_pc.save07
ifs.loadsave_pc.save08 ifs.loadsave_pc.save09 ifs.loadsave_pc.save10 ifs.loadsave_pc.save11
ifs.loadsave_pc.save13 ifs.loadsave_pc.save14 ifs.loadsave_pc.save15 ifs.loadsave_pc.save16
ifs.loadsave_pc.save17 ifs.loadsave_pc.save18 ifs.loadsave_pc.save19 ifs.loadsave_pc.save20
ifs.loadsave_pc.save21 ifs.loadsave_pc.save22 ifs.loadsave_pc.save23 ifs.loadsave_pc.yes
ifs.loadsave_ps2.cancel ifs.loadsave_ps2.continue ifs.loadsave_ps2.continuenosave
ifs.loadsave_ps2.errprefixdelete ifs.loadsave_ps2.errprefixformat ifs.loadsave_ps2.errprefixload
ifs.loadsave_ps2.errprefixsave ifs.loadsave_ps2.exitnosave ifs.loadsave_ps2.no ifs.loadsave_ps2.ok
ifs.loadsave_ps2.save ifs.loadsave_ps2.save02 ifs.loadsave_ps2.save03 ifs.loadsave_ps2.save04
ifs.loadsave_ps2.save05 ifs.loadsave_ps2.save06 ifs.loadsave_ps2.save07 ifs.loadsave_ps2.save08
ifs.loadsave_ps2.save09 ifs.loadsave_ps2.save10 ifs.loadsave_ps2.save11 ifs.loadsave_ps2.save13
ifs.loadsave_ps2.save14 ifs.loadsave_ps2.save15 ifs.loadsave_ps2.save16 ifs.loadsave_ps2.save17
ifs.loadsave_ps2.save18 ifs.loadsave_ps2.save19 ifs.loadsave_ps2.save20 ifs.loadsave_ps2.save21
ifs.loadsave_ps2.save22 ifs.loadsave_ps2.save23 ifs.loadsave_ps2.save24 ifs.loadsave_ps2.save25
ifs.loadsave_ps2.save26 ifs.loadsave_ps2.saveandexit ifs.loadsave_ps2.yes ifs.loadsave_xbox.cancel
ifs.loadsave_xbox.continue ifs.loadsave_xbox.no ifs.loadsave_xbox.ok ifs.loadsave_xbox.save02
ifs.loadsave_xbox.save03 ifs.loadsave_xbox.save04 ifs.loadsave_xbox.save05 ifs.loadsave_xbox.save06
ifs.loadsave_xbox.save07 ifs.loadsave_xbox.save08 ifs.loadsave_xbox.save09 ifs.loadsave_xbox.save10
ifs.loadsave_xbox.save11 ifs.loadsave_xbox.save13 ifs.loadsave_xbox.save14 ifs.loadsave_xbox.save15
ifs.loadsave_xbox.save16 ifs.loadsave_xbox.save17 ifs.loadsave_xbox.save18 ifs.loadsave_xbox.save19
ifs.loadsave_xbox.save20 ifs.loadsave_xbox.save21 ifs.loadsave_xbox.save22 ifs.loadsave_xbox.save23
ifs.loadsave_xbox.yes ifs.main.askquit ifs.main.mp ifs.main.options ifs.main.profiles ifs.main.sp
ifs.main.split ifs.main.title ifs.main.tutorials ifs.main.xlive ifs.meta.alreadyown
ifs.meta.battle.active ifs.meta.battle.battlefor ifs.meta.battle.deathstar1
ifs.meta.battle.deathstar2 ifs.meta.battle.lost ifs.meta.battle.none ifs.meta.battle.vsstr
ifs.meta.bonus.apply_all ifs.meta.bonus.apply_cis ifs.meta.bonus.apply_imp ifs.meta.bonus.apply_rep
ifs.meta.bonus.endor_no ifs.meta.bonus.endor_yes ifs.meta.bonus.nobonus ifs.meta.bonus.none
ifs.meta.bonus.select ifs.meta.bonus.title ifs.meta.configs.1 ifs.meta.configs.2 ifs.meta.configs.3
ifs.meta.configs.4 ifs.meta.configs.5 ifs.meta.configs.6 ifs.meta.configs.7 ifs.meta.configs.8
ifs.meta.configs.detail ifs.meta.configs.era ifs.meta.configs.text ifs.meta.load.btnload
ifs.meta.load.btnsave ifs.meta.load.confirm_delete ifs.meta.load.confirm_save ifs.meta.load.load
ifs.meta.load.new ifs.meta.load.nogames ifs.meta.load.save ifs.meta.load.saveasnew
ifs.meta.main.attackedlastturn ifs.meta.main.attacker_choice ifs.meta.main.charge
ifs.meta.main.contest ifs.meta.main.enemy ifs.meta.main.infobox ifs.meta.main.own
ifs.meta.main.selattack ifs.meta.main.selconfig ifs.meta.movie.text ifs.meta.popups.dedicated
ifs.meta.popups.isnow ifs.meta.popups.notarget ifs.meta.popups.reallyapply ifs.meta.popups.winbattle
ifs.meta.popups.wonthegame ifs.meta.presstoattack ifs.meta.title ifs.meta.top.subtitle
ifs.missionselect.all ifs.missionselect.available ifs.missionselect.cis ifs.missionselect.imp
ifs.missionselect.inorder ifs.missionselect.launch ifs.missionselect.listfull
ifs.missionselect.random ifs.missionselect.rep ifs.missionselect.selected
ifs.missionselect.serveroptions ifs.missionselect.title ifs.mp.badconnection ifs.mp.badhost
ifs.mp.connection.direct ifs.mp.connection.galaxy ifs.mp.connection.gamespy ifs.mp.connection.lan
ifs.mp.connection.split ifs.mp.connection.title ifs.mp.connection.xlive ifs.mp.create.entername
ifs.mp.create.numbots ifs.mp.create.title ifs.mp.createopts.autoaim ifs.mp.createopts.autoassign_off
ifs.mp.createopts.autoassign_on ifs.mp.createopts.dedicated ifs.mp.createopts.difficulty
ifs.mp.createopts.herodisable ifs.mp.createopts.heroenable ifs.mp.createopts.invincibletime
ifs.mp.createopts.maxplayers ifs.mp.createopts.numbots ifs.mp.createopts.password
ifs.mp.createopts.ping ifs.mp.createopts.pubpriv_priv ifs.mp.createopts.pubpriv_pub
ifs.mp.createopts.shownames ifs.mp.createopts.startcnt ifs.mp.createopts.teamdamage
ifs.mp.createopts.title ifs.mp.createopts.version ifs.mp.dssessionlist.title ifs.mp.galaxy.error
ifs.mp.galaxy.signin_connection_failure ifs.mp.galaxy.signin_invalid_credentials
ifs.mp.galaxy.signin_no_license ifs.mp.galaxy.signin_service_not_available
ifs.mp.galaxy.signin_service_not_signed_in ifs.mp.galaxy.signin_undefined ifs.mp.join.login_as
ifs.mp.joinerrors.banned ifs.mp.joinerrors.booted ifs.mp.joinerrors.busy ifs.mp.joinerrors.cantfind
ifs.mp.joinerrors.cdkey ifs.mp.joinerrors.connectlost ifs.mp.joinerrors.full ifs.mp.joinerrors.game
ifs.mp.joinerrors.hostquit ifs.mp.joinerrors.missingdc ifs.mp.joinerrors.password
ifs.mp.joinerrors.version ifs.mp.no_net_adaptor ifs.mp.sessionlist.era ifs.mp.sessionlist.gameinfo
ifs.mp.sessionlist.gamename ifs.mp.sessionlist.map ifs.mp.sessionlist.map_missing
ifs.mp.sessionlist.numplayers ifs.mp.sessionlist.ping ifs.mp.sessionlist.refresh
ifs.mp.sessionlist.reinforcements ifs.mp.sessionlist.servertype ifs.mp.sessionlist.servertypes.pc
ifs.mp.sessionlist.servertypes.pcdedicated ifs.mp.sessionlist.servertypes.ps2
ifs.mp.sessionlist.servertypes.ps2dedicated ifs.mp.sessionlist.servertypes.xbox
ifs.mp.sessionlist.servertypes.xboxdedicated ifs.mp.sessionlist.sortby ifs.mp.sessionlist.title
ifs.mp.sessionlist.totalservers ifs.mp_lobbyds.title ifs.mplobby.client_title ifs.mplobby.host_title
ifs.mplobby.name_header ifs.mplobby.ping_header ifs.mplobby.qos_header ifs.mplobby.team_header
ifs.onlinelobby.addfriend ifs.onlinelobby.addmute ifs.onlinelobby.boot ifs.onlinelobby.cancelsession
ifs.onlinelobby.changeteam ifs.onlinelobby.delmute ifs.onlinelobby.filters ifs.onlinelobby.forceboot
ifs.onlinelobby.friendslist ifs.onlinelobby.launch ifs.onlinelobby.leavesession ifs.onlinelobby.map
ifs.onlinelobby.options ifs.onlinelobby.ping ifs.onlinelobby.playeropts ifs.onlinelobby.players
ifs.onlinelobby.powerups ifs.onlinelobby.ready ifs.onlinelobby.sendfeedback ifs.onlinelobby.status
ifs.onlinelobby.title ifs.onlinelobby.unmuted ifs.onlinelobby.vote ifs.onlinelobby.wrongver
ifs.onlineopt.appear ifs.onlineopt.autologin ifs.onlineopt.bandwidth1 ifs.onlineopt.bandwidth2
ifs.onlineopt.bandwidth3 ifs.onlineopt.bandwidth4 ifs.onlineopt.bandwidth5 ifs.onlineopt.bandwidth6
ifs.onlineopt.hostbandwidth ifs.onlineopt.icon ifs.onlineopt.offline ifs.onlineopt.online
ifs.onlineopt.title ifs.onlineopt.tps ifs.onlineopt.voiceenable ifs.onlineopt.voicemask
ifs.onlineopt.voiceplaybackvol ifs.onlineopt.voicerecordvol ifs.onlineopt.voicetotvvolume
ifs.pause.warn_host_quit ifs.pause.warn_quit ifs.pause.warn_quit_meta ifs.pause.warn_restart
ifs.pckeyboard.entername ifs.pickacct.asksignout ifs.profile.confirm_delete ifs.profile.create
ifs.profile.delete ifs.profile.dupname ifs.profile.list ifs.profile.nametemplate
ifs.profile.selection ifs.profile.title ifs.quickopti.nonefound ifs.soundopt.bassmanagement
ifs.soundopt.eax.envmorphing ifs.soundopt.eax.envpanning ifs.soundopt.eax.occlusion
ifs.soundopt.eax.reverb ifs.soundopt.effects.off ifs.soundopt.effects.on ifs.soundopt.effects.title
ifs.soundopt.mastervol ifs.soundopt.mixconfig.disabled ifs.soundopt.mixconfig.hardware
ifs.soundopt.mixconfig.software ifs.soundopt.mixconfig.softwareal ifs.soundopt.mixconfig.title
ifs.soundopt.musicvol ifs.soundopt.outputmode.4point0 ifs.soundopt.outputmode.5point1
ifs.soundopt.outputmode.7point1 ifs.soundopt.outputmode.dolby ifs.soundopt.outputmode.headphones
ifs.soundopt.outputmode.mono ifs.soundopt.outputmode.stereo ifs.soundopt.outputmode.surround
ifs.soundopt.outputmode.title ifs.soundopt.sfxvol ifs.soundopt.speechvol ifs.soundopt.title
ifs.sp.campaign ifs.sp.campaign.cw ifs.sp.campaign.gcw ifs.sp.cw.map1descr ifs.sp.cw.map1name
ifs.sp.cw.map2descr ifs.sp.cw.map2name ifs.sp.cw.map3descr ifs.sp.cw.map3name ifs.sp.cw.map4descr
ifs.sp.cw.map4name ifs.sp.cw.map5descr ifs.sp.cw.map5name ifs.sp.cw.map6descr ifs.sp.cw.map6name
ifs.sp.cw.map7descr ifs.sp.cw.map7name ifs.sp.gcw.map1descr ifs.sp.gcw.map1name ifs.sp.gcw.map2descr
ifs.sp.gcw.map2name ifs.sp.gcw.map3descr ifs.sp.gcw.map3name ifs.sp.gcw.map4descr
ifs.sp.gcw.map4name ifs.sp.gcw.map5descr ifs.sp.gcw.map5name ifs.sp.gcw.map6descr
ifs.sp.gcw.map6name ifs.sp.gcw.map7descr ifs.sp.gcw.map7name ifs.sp.gcw.map8descr
ifs.sp.gcw.map8name ifs.sp.gcw.map9descr ifs.sp.gcw.map9name ifs.sp.ia ifs.sp.meta ifs.sp.pick_era
ifs.sp.title ifs.spawndisplay.spawn ifs.split.insertcontrollerport ifs.split.lan
ifs.split.needs2controllers ifs.split.online ifs.split.ready ifs.split.starttojoin
ifs.split.starttolaunch ifs.split.title ifs.start.gotinvite ifs.start.nocontroller
ifs.start.press_start ifs.stats.accuracy ifs.stats.awards ifs.stats.awardstatstitle ifs.stats.back
ifs.stats.bait ifs.stats.baitkills ifs.stats.camper ifs.stats.commandpostpoints ifs.stats.deadeye
ifs.stats.deaths ifs.stats.details ifs.stats.done ifs.stats.error.notavailable
ifs.stats.error.usernotfound ifs.stats.experience ifs.stats.favclass ifs.stats.favoritestitle
ifs.stats.favvehicle ifs.stats.favweapon ifs.stats.generaltitle ifs.stats.headshots
ifs.stats.killingspree ifs.stats.kills ifs.stats.lastgameplayed ifs.stats.level
ifs.stats.livingstreak ifs.stats.losses ifs.stats.meat ifs.stats.misctitle ifs.stats.narrowescapes
ifs.stats.nemesis ifs.stats.nemesisdeaths ifs.stats.notavailable ifs.stats.overall
ifs.stats.personalstatstitle ifs.stats.pointman ifs.stats.publicenemy ifs.stats.skill
ifs.stats.statstitle ifs.stats.survivalist ifs.stats.tankbuster ifs.stats.title
ifs.stats.totaltimeplayed ifs.stats.traitor ifs.stats.vehiclesdestroyed ifs.stats.wins
ifs.subharddrive.busy ifs.tutorials.name1 ifs.tutorials.name2 ifs.tutorials.name3
ifs.tutorials.name4 ifs.tutorials.name5 ifs.tutorials.name6 ifs.tutorials.name7 ifs.tutorials.name8
ifs.tutorials.title ifs.unlock.body ifs.unlock.name1 ifs.unlock.name10 ifs.unlock.name2
ifs.unlock.name3 ifs.unlock.name4 ifs.unlock.name5 ifs.unlock.name6 ifs.unlock.name7
ifs.unlock.name8 ifs.unlock.name9 ifs.unlock.notyet ifs.unlock.title ifs.unlock.view
ifs.videoopt.autodetect ifs.videoopt.brightness ifs.videoopt.cancel ifs.videoopt.contrast
ifs.videoopt.distortion ifs.videoopt.high ifs.videoopt.low ifs.videoopt.med ifs.videoopt.motionblur
ifs.videoopt.resolution ifs.videoopt.shadows ifs.videoopt.terrainquality ifs.videoopt.texturedetail
ifs.videoopt.title ifs.videoopt.waterquality ifs.videopt.bumpmapping ifs.videopt.loddistance
ifs.videopt.multisample ifs.videopt.particlequality ifs.videopt.resolution ifs.videopt.shadows
ifs.videopt.specularquality ifs.videopt.viewdistance ifs.videopt.vsync ifs.vkeyboard.backspace
ifs.vkeyboard.backspace_large ifs.vkeyboard.mode ifs.vkeyboard.select ifs.vkeyboard.title_gamename
ifs.vkeyboard.title_password ifs.vkeyboard.tooshort level.bes1.objectives.1 level.bes1.objectives.2
level.bes1.objectives.3 level.bespin2.objectives.1 level.bespin2.objectives.2
level.end1.objectives.1 level.end1.objectives.2 level.end1.objectives.3 level.geo1.objectives.1r
level.geo1.objectives.2r level.geo1.objectives.3r level.geo1.objectives.4r level.geo1.objectives.5r
level.hoth.objectives.1 level.hoth.objectives.2 level.hoth.objectives.3 level.hoth.objectives.4
level.kamino1.objectives.1 level.kas2.objectives.1a level.kas2.objectives.1c
level.kas2.objectives.1h level.kas2.objectives.1i level.kas2.objectives.1r level.kas2.objectives.2a
level.kas2.objectives.2c level.kas2.objectives.2h level.kas2.objectives.2i level.kas2.objectives.2r
level.kas2.objectives.3a level.kas2.objectives.3c level.kas2.objectives.3h level.kas2.objectives.3i
level.kas2.objectives.3r level.kashyyyk1.objectives.1 level.naboo1.objectives.1a
level.naboo1.objectives.2a level.naboo1.objectives.2h level.naboo1.objectives.2r
level.naboo1.objectives.3a level.naboo1.objectives.3h level.naboo1.objectives.3r
level.naboo1.objectives.4a level.naboo1.objectives.4h level.naboo1.objectives.4r
level.naboo1.objectives.5a level.naboo1.objectives.5h level.naboo1.objectives.5r level.naboo2.hints
level.naboo2.objectives.1 level.naboo2.objectives.2 level.naboo2.objectives.3
level.rhenvar1.objectives.1 level.rhenvar1.objectives.2 level.rhenvar1.objectives.3
level.rhenvar2.objectives.1 level.rhenvar2.objectives.2 level.rhenvar2.objectives.3
level.tat1.objectives.1 level.tat1.objectives.2 level.tat2.objectives.1 level.tat2.objectives.2
level.tat2.objectives.3 level.yav2.objectives.1 level.yav2.objectives.2 level.yav2.objectives.3
level.yav2.objectives.4 level.yav2.objectives.5 level.yav2.objectives.6 level.yavin1.objectives.1
planets.bespin.gar_weap_inf_thermaldetonator planets.bespin.longbonus planets.bespin.mapname1
planets.bespin.mapname2 planets.bespin.name planets.bespin.shortbonus planets.endor.longbonus
planets.endor.mapname1 planets.endor.name planets.endor.shortbonus planets.geonosis.longbonus
planets.geonosis.mapname1 planets.geonosis.name planets.geonosis.shortbonus planets.hoth.longbonus
planets.hoth.mapname1 planets.hoth.name planets.hoth.shortbonus planets.kamino.longbonus
planets.kamino.mapname1 planets.kamino.name planets.kamino.shortbonus planets.kashyyyk.longbonus
planets.kashyyyk.mapname1 planets.kashyyyk.mapname2 planets.kashyyyk.name
planets.kashyyyk.shortbonus planets.naboo.longbonus planets.naboo.mapname1 planets.naboo.mapname2
planets.naboo.name planets.naboo.shortbonus planets.rhenvar.longbonus planets.rhenvar.mapname1
planets.rhenvar.mapname2 planets.rhenvar.name planets.rhenvar.shortbonus planets.tatooine.longbonus
planets.tatooine.mapname1 planets.tatooine.mapname2 planets.tatooine.name
planets.tatooine.shortbonus planets.yavin.longbonus planets.yavin.mapname1 planets.yavin.mapname2
planets.yavin.name planets.yavin.shortbonus
""".split()
learn_keys(KEY_NAMES)


# ==================================================================================================
# bf1_media - merged in from bf1_media.py
# ==================================================================================================
# bf1_media.py - list and extract the game's movies (.mvs) and sound banks (.bnk).
#
# Both are UCFB files like a .lvl, but every tag and field name is stored as
# an FNV-1a hash (bf1_core.fnv1a32) instead of text - cracked by hashing
# likely names:
#
#   .mvs  ucfb > [hash] > [hash] > info, data
#         info = name <file>, numsegments N, segmentinfo,
#                N x (segment, name <movie>, length L, padding P, segmentend)
#         data = the movies back to back, each a complete Bink video (BIKi)
#                followed by P bytes of padding (L + P is a multiple of 2048)
#
#   .bnk  ucfb > [hash] > samplebank > info, data
#         info = name, format 2, numsamples N, size <total>, ..., sampleinfo,
#                N x (sample, id <sample>, frequency Hz, size bytes,
#                     sizesamples frames, [alias <other id>], padding, sampleend)
#         data = 16-bit mono PCM, the samples back to back in info order.
#                An 'alias' sample has no audio of its own - it plays the
#                sample it names (checked: the alias sizes account exactly for
#                the gap between the summed sizes and the bank's total).
#
# Names are only stored as hashes. With Phantom's dictionary.txt next to the tools
# (see sound_names) every stock sound and movie gets its real name; without it,
# KNOWN_NAMES holds the ones recovered by
# hashing every string in the stock game's .lvl files and the map naming pattern (32 of
# shell.mvs's 55 movies, plus tat3fly); everything else is named by its hash.



_H = {name: fnv1a32(name) for name in (
    "info", "data", "name", "numsegments", "segmentinfo", "segment", "length", "padding", "segmentend",
    "format", "numsamples", "size", "sampleinfo", "sample", "id", "frequency", "sizesamples", "alias",
    "sampleend", "samplebank", "soundbanklist")}
_MARKERS = {_H[k] for k in ("segmentinfo", "segment", "segmentend", "sampleinfo", "sample", "sampleend")}

# hash -> name, recovered from the stock game's own strings (see module docstring)
KNOWN_NAMES = {fnv1a32(n): n for n in (
    "main", "bes1fly", "bes1h01", "bes2fly", "bes2h01", "end1fly", "end1h02", "end1h03", "geo1fly", "geo1h01",
    "hot1fly", "hot1h02", "kam1fly", "kam1h01", "kam1h02", "kas1fly", "kas2fly", "kas2h01", "nab1fly",
    "nab1h01", "nab2fly", "nab2h01", "rhn1fly", "rhn2fly", "tat1fly", "tat1h01", "tat2fly", "tat2h01",
    "tat3fly", "yav1fly", "yav1h01", "yav2fly", "yav2h01")}


class MediaError(Exception):
    pass


DICTIONARY_FILE = "dictionary.txt"
DICTIONARY_URL = "https://github.com/phantom567459/SoundFMVextractor"
_sound_names = None


def sound_names() -> dict:
    """hash -> name from a dictionary.txt next to the tools: the list of known
    SWBF sound and movie file names from Phantom's SoundFMVextractor (GPL-3.0,
    so it isn't bundled - see DICTIONARY_URL). It names every sample in the
    stock common.bnk and every stock movie. Empty when the file isn't there."""
    global _sound_names
    if _sound_names is None:
        _sound_names = {}
        path = app_dir() / DICTIONARY_FILE
        if path.is_file():
            for line in path.read_text(encoding="ascii", errors="replace").splitlines():
                line = line.strip()
                if line:
                    _sound_names.setdefault(fnv1a32(line), line)
    return _sound_names


def reload_sound_names() -> int:
    """Re-reads dictionary.txt (e.g. after it was added); returns how many names it has."""
    global _sound_names
    _sound_names = None
    return len(sound_names())


PCM_FORMAT = 2      # a sample bank's 'format' for 16-bit mono PCM (all of common.bnk)
MAX_RATE = 44100    # highest sample rate the stock game uses


@dataclass
class MediaEntry:
    hash: int
    offset: int        # absolute position in the file
    length: int        # bytes
    frequency: int = 0  # sounds only (Hz)
    alias_of: int | None = None

    @property
    def name(self) -> str:
        return KNOWN_NAMES.get(self.hash) or sound_names().get(self.hash) or f"{self.hash:08x}"

    @property
    def seconds(self) -> float:
        return self.length / 2 / self.frequency if self.frequency else 0.0


@dataclass
class MediaFile:
    path: Path
    kind: str                     # "movies" or "sounds"
    entries: list = field(default_factory=list)

    @property
    def extension(self) -> str:
        return ".bik" if self.kind == "movies" else ".wav"


def _chunks(f, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        f.seek(pos)
        tag, size = struct.unpack("<II", f.read(8))
        yield tag, pos + 8, size
        pos += 8 + size + (-size) % 4


def _fields(raw: bytes) -> list:
    """The info block as [(key hash, value or None)] - section markers have no value."""
    words = struct.unpack_from(f"<{len(raw) // 4}I", raw)
    out, i = [], 0
    while i < len(words):
        if words[i] in _MARKERS:
            out.append((words[i], None))
            i += 1
        else:
            out.append((words[i], words[i + 1] if i + 1 < len(words) else None))
            i += 2
    return out


def _records(fields: list, begin: int, end: int) -> list:
    records, current = [], None
    for key, value in fields:
        if key == begin:
            current = {}
        elif key == end:
            if current is not None:
                records.append(current)
            current = None
        elif current is not None:
            current[key] = value
    return records


def _media_chunk(f, total: int) -> int:
    """File position of the chunk holding the movies/samples: the only chunk
    of a .mvs/.bnk, or the SoundBankList chunk of a .lvl - core.lvl carries
    the menu sounds (ui_menuMove, ui_cancel...) in one, and the game plays
    those instead of common.bnk's copies."""
    first = None
    for tag, pos, _size in _chunks(f, 8, total):
        if tag == _H["soundbanklist"]:
            return pos - 8
        if first is None:
            first = pos - 8
    if first is None:
        raise MediaError("The file is empty.")
    return first


def read_media(path) -> MediaFile:
    """Reads the table of contents (not the media itself) of a .mvs, a .bnk,
    or the sound bank inside a .lvl such as core.lvl."""
    path = Path(path)
    with open(path, "rb") as f:
        if f.read(4) != b"ucfb":
            raise MediaError(f"{path.name} isn't a UCFB file.")
        total = path.stat().st_size
        _, outer, outer_size = next(_chunks(f, _media_chunk(f, total), total))
        _, inner, inner_size = next(_chunks(f, outer, outer + outer_size))
        parts = {tag: (pos, size) for tag, pos, size in _chunks(f, inner, inner + inner_size)}
        if _H["info"] not in parts or _H["data"] not in parts:
            raise MediaError(f"{path.name} has no info/data block - not a movie or sound bank file.")
        info_pos, info_size = parts[_H["info"]]
        data_pos, data_size = parts[_H["data"]]
        f.seek(info_pos)
        fields = _fields(f.read(info_size))

        segments = _records(fields, _H["segment"], _H["segmentend"])
        samples = _records(fields, _H["sample"], _H["sampleend"])
        if segments:
            media, pos = MediaFile(path, "movies"), data_pos
            for rec in segments:
                media.entries.append(MediaEntry(rec[_H["name"]], pos, rec[_H["length"]]))
                pos += rec[_H["length"]] + rec.get(_H["padding"], 0)
            if pos - data_pos > data_size:
                raise MediaError("The movie table doesn't fit the data - unexpected .mvs layout.")
            return media
        if samples:
            fmt = next((v for k, v in fields if k == _H["format"]), None)
            if fmt != PCM_FORMAT:
                # the per-map Sound\*.lvl banks are format 5: compressed 4-bit streams (music,
                # ambience) - reading those as PCM would just give noise
                raise MediaError(f"{path.name} holds compressed audio streams (format {fmt}), which aren't "
                                 f"supported yet - only plain sample banks like common.bnk are.")
            media, pos, by_id = MediaFile(path, "sounds"), data_pos, {}
            for rec in samples:
                entry = MediaEntry(rec[_H["id"]], pos, rec[_H["size"]], rec.get(_H["frequency"], 22050),
                                   rec.get(_H["alias"]))
                if entry.alias_of is None:
                    pos += entry.length + rec.get(_H["padding"], 0)
                media.entries.append(entry)
                by_id[entry.hash] = entry
            for entry in media.entries:  # aliases play another sample's audio
                if entry.alias_of is not None and entry.alias_of in by_id:
                    target = by_id[entry.alias_of]
                    entry.offset, entry.length = target.offset, min(entry.length, target.length)
            if pos - data_pos > data_size:
                raise MediaError("The sample table doesn't fit the data - unexpected .bnk layout.")
            return media
        raise MediaError(f"{path.name} lists no movies or sounds.")


def entry_bytes(media: MediaFile, entry: MediaEntry) -> bytes:
    """The entry as a ready-to-save file: a .bik for movies, a .wav for sounds."""
    with open(media.path, "rb") as f:
        f.seek(entry.offset)
        raw = f.read(entry.length)
    if media.kind == "movies":
        return raw
    return pcm_to_wav(raw, entry.frequency or 22050)


def pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    """16-bit mono PCM as a .wav file."""
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return out.getvalue()


def extract(media: MediaFile, out_dir, entries=None, progress=None) -> list:
    """Writes `entries` (default: all) into `out_dir` - movies as .bik, sounds
    as .wav; returns the paths. `progress(done, total)` is called after each file."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    chosen = list(entries if entries is not None else media.entries)
    written = []
    for i, entry in enumerate(chosen):
        target = out_dir / (entry.name + media.extension)
        target.write_bytes(entry_bytes(media, entry))
        written.append(target)
        if progress:
            progress(i + 1, len(chosen))
    return written


# --- putting sounds back ----------------------------------------------------------------

def load_audio(path, target_rate: int | None = None) -> tuple:
    """(16-bit mono PCM bytes, sample rate) from a .wav file: 8/16/24/32-bit
    whole-number samples, mono or stereo (mixed down). Rates above 44.1 kHz
    come down to 44.1 kHz, the highest the game uses. Other formats (mp3, ogg,
    a 32-bit float .wav) need converting to a plain .wav first, e.g. in Audacity.
    `target_rate` is accepted for compatibility and unused: the file's own rate is kept."""
    import numpy as np
    path = Path(path)
    if path.suffix.lower() != ".wav":
        raise MediaError(f"{path.name}: sounds have to be .wav files. Convert it to a 16-bit .wav first "
                         f"(Audacity: File > Export > WAV).")
    try:
        with wave.open(str(path), "rb") as w:
            channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
            raw = w.readframes(w.getnframes())
    except (wave.Error, EOFError) as exc:
        raise MediaError(f"{path.name}: can't read this .wav ({exc}). Save it as a 16-bit PCM .wav "
                         f"(32-bit float isn't supported).")
    if width == 1:
        samples = (np.frombuffer(raw, np.uint8).astype(np.float64) - 128) * 256
    elif width == 3:
        b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
        samples = ((b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)) << 8 >> 8).astype(np.float64) / 256
    elif width in (2, 4):
        samples = np.frombuffer(raw, np.int16 if width == 2 else np.int32).astype(np.float64)
        if width == 4:
            samples /= 65536
    else:
        raise MediaError(f"{path.name}: {width * 8}-bit audio isn't supported.")
    samples = samples.reshape(-1, channels).mean(axis=1)  # mix down to mono
    if rate > MAX_RATE:
        n = int(len(samples) * MAX_RATE / rate)
        samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
        rate = MAX_RATE
    return np.clip(np.round(samples), -32768, 32767).astype("<i2").tobytes(), rate


def rebuild_bank(path, replacements: dict) -> bytes:
    """A sound bank's bytes with samples replaced: `replacements` maps a
    sample's hash to (16-bit mono PCM bytes, sample rate). Everything else
    is kept byte for byte; the sample table, the bank's total size and the
    chunk sizes are updated, and the audio block keeps the stock 2048-byte
    alignment. Aliases of a replaced sample follow it. With no replacements
    the result is identical to the original file."""
    data = Path(path).read_bytes()
    try:
        if data[:4] != b"ucfb":
            raise ValueError
        bank = _media_chunk(io.BytesIO(data), len(data))  # 8 in a .bnk; the SoundBankList chunk in a .lvl
        outer_tag, outer_size = struct.unpack_from("<II", data, bank)
        inner_tag, inner_size = struct.unpack_from("<II", data, bank + 8)
        info_tag, info_size = struct.unpack_from("<II", data, bank + 16)
        data_at = bank + 24 + info_size
        data_tag, data_size = struct.unpack_from("<II", data, data_at)
    except (ValueError, struct.error, MediaError):
        raise MediaError(f"{Path(path).name} has no sound bank.")
    bank_end = bank + 8 + outer_size
    if not (info_tag == _H["info"] and data_tag == _H["data"] and inner_size == 16 + info_size + data_size
            and outer_size == inner_size + 8 and bank_end <= len(data) and info_size % 4 == 0):
        raise MediaError(f"{Path(path).name} has an unexpected layout - can't safely rewrite it.")
    words = list(struct.unpack_from(f"<{info_size // 4}I", data, bank + 24))

    # walk the table, remembering where each value lives so it can be patched in place
    records, current, total_at, in_samples, i = [], None, None, False, 0
    while i < len(words):
        key = words[i]
        if key in _MARKERS:
            if key == _H["sampleinfo"]:
                in_samples = True
            elif key == _H["sample"]:
                current = {}
            elif key == _H["sampleend"] and current is not None:
                records.append(current)
                current = None
            i += 1
            continue
        if current is not None:
            current[key] = i + 1
        elif not in_samples and key == _H["size"] and total_at is None:
            total_at = i + 1
        i += 2
    if total_at is None or not records:
        raise MediaError(f"{Path(path).name} has no sample table.")

    value = lambda rec, key: words[rec[key]] if key in rec else 0
    audio, pos, new_by_id = [], bank + 32 + info_size, {}
    for rec in records:
        if _H["alias"] in rec:
            continue
        sample_id, size, padding = value(rec, _H["id"]), value(rec, _H["size"]), value(rec, _H["padding"])
        pcm, rate = replacements.get(sample_id, (data[pos:pos + size], value(rec, _H["frequency"])))
        pos += size + padding
        audio.append(pcm + b"\0" * padding)
        new_by_id[sample_id] = (len(pcm), rate)
        words[rec[_H["size"]]] = len(pcm)
        words[rec[_H["sizesamples"]]] = len(pcm) // 2
        if _H["frequency"] in rec:
            words[rec[_H["frequency"]]] = rate
    for rec in records:  # an alias plays its target, so it takes the target's new length and rate
        if _H["alias"] in rec and value(rec, _H["alias"]) in new_by_id:
            size, rate = new_by_id[value(rec, _H["alias"])]
            words[rec[_H["size"]]] = size
            words[rec[_H["sizesamples"]]] = size // 2
            if _H["frequency"] in rec:
                words[rec[_H["frequency"]]] = rate
    blob = b"".join(audio)
    words[total_at] = sum(size for size, _ in new_by_id.values())
    # stock banks pad the audio to whole 2048-byte blocks, and the padding opens with a
    # record of its own: 'padding' + (padding size - 16) - same in the stock and a modded bank
    pad = -len(blob) % 2048
    if pad < 16:
        pad += 2048
    blob += struct.pack("<II", _H["padding"], pad - 16) + b"\0" * (pad - 8)

    info = struct.pack(f"<{len(words)}I", *words)
    inner = struct.pack("<II", info_tag, len(info)) + info + struct.pack("<II", data_tag, len(blob)) + blob
    outer = struct.pack("<II", inner_tag, len(inner)) + inner
    new_bank = struct.pack("<II", outer_tag, len(outer)) + outer
    # the bank ends on a 2048-byte boundary before and after, so whatever follows it in a
    # .lvl (core.lvl's snd_, fonts, textures...) moves by whole blocks and keeps its alignment
    result = bytearray(data[:bank] + new_bank + data[bank_end:])
    struct.pack_into("<I", result, 4, struct.unpack_from("<I", data, 4)[0] + len(new_bank) - (bank_end - bank))
    return bytes(result)


# ==================================================================================================
# bf1_glb_import - merged in from bf1_glb_import.py
# ==================================================================================================
# bf1_glb_import.py - Import a .glb (binary glTF 2.0) mesh into an existing
# 'modl' chunk, replacing its geometry while preserving materials/textures.
#
# WHY THIS IS SCOPED THE WAY IT IS
# ---------------------------------
# Reverse-engineered against swbf-unmunge's own source
# (github.com/PrismaticFlower/swbf-unmunge, src/handle_model.cpp and
# src/vbuf_reader.cpp - the exe bundled in this repo is built from that
# project, confirmed by its embedded .pdb path), not guessed:
#
# - A 'modl' chunk is NAME + NODE(s) + INFO (bounding boxes/face count) +
#   one or more 'segm' children (one per material) + SPHR. Each 'segm' is
#   INFO (topology/vertex_count/primitive_count) + MTRL (material) + RTYP
#   (render type string) + TNAM (texture name(s)) + IBUF (index buffer) +
#   one-or-more VBUF (vertex buffer variants) + BNAM (bone name).
# - The 'gmod' chunk sitting next to it (same name) is NEVER READ by
#   swbf-unmunge's model-handling code at all (confirmed by grepping
#   handle_model.cpp) - whatever it's for, it isn't part of the renderable
#   mesh, so it's safe to leave completely untouched here.
# - VBUF's binary layout (confirmed against real game data in this repo,
#   byte-for-byte, via vbuf_reader.cpp's Vbuf_flags/Vbuf_info): a 12-byte
#   header (count, stride, flags bitmask) followed by per-vertex data whose
#   layout depends on the flags. This importer only ever *writes* the
#   simplest supported variant - flags 0x222 (position | normal | texcoords,
#   all uncompressed float) at a 32-byte stride - which this repo's own
#   sample data confirms is a real, accepted variant (present as the second
#   VBUF alongside a compressed one in every segment checked).
#
# Given that, this importer intentionally does NOT attempt to build a modl
# chunk from scratch (materials, render flags, texture bindings, bone
# weights, and the 'coll' collision BVH are a much bigger surface, and
# getting any of it wrong risks a corrupt/unloadable level). Instead it
# replaces an EXISTING model's geometry - one GLB mesh primitive per
# existing 'segm', matched by order - while copying that segment's MTRL/
# RTYP/TNAM/BNAM (and anything else in it this module doesn't recognize)
# through unchanged. This is intentionally the same "safe subset" tradeoff
# as the texture codec: lossy/limited, but every byte it writes is for a
# field whose meaning and encoding has actually been verified, not guessed.
#
# CAVEATS (documented, not hidden):
# - Vertex normals are computed from the mesh only if the GLB provides them;
#   otherwise flat per-face normals are synthesized.
# - UVs default to (0, 0) if the GLB primitive has no TEXCOORD_0.
# - The 'coll' collision mesh (if any) is left pointing at the OLD geometry
#   - it is not regenerated from the new mesh.
# - Skinned character models (soldier bodies) are refused here - their
#   segments carry per-vertex bone data this module doesn't rebuild. See
#   bf1_skinned_import.py for those.



_COMPONENT_FORMATS = {
    5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2),
    5125: ("I", 4), 5126: ("f", 4),
}
_TYPE_COMPONENT_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class GlbImportError(Exception):
    pass


class GlbCountMismatch(GlbImportError):
    def __init__(self, segments: int, primitives: int):
        self.segments = segments
        self.primitives = primitives
        super().__init__(
            f"This model has {segments} material segment(s) but the .glb has {primitives} mesh "
            "primitive(s) - they should match 1:1, in order, so each segment's existing "
            "material/texture stays with the geometry it's meant for.")


@dataclass
class Primitive:
    positions: list  # [(x, y, z), ...]
    normals: list
    texcoords: list
    indices: list  # flat triangle-list indices (already un-stripped/un-fanned)
    texture_name: str = ""  # .glb material name, used only when building a new modl


def parse_glb(data: bytes) -> tuple[dict, bytes]:
    if data[:4] != b"glTF":
        raise GlbImportError("Not a .glb file (missing 'glTF' magic).")
    _magic, _version, total_len = struct.unpack_from("<4sII", data, 0)
    pos = 12
    gltf = None
    bin_chunk = b""
    while pos < total_len:
        chunk_len, chunk_type = struct.unpack_from("<I4s", data, pos)
        chunk_data = data[pos + 8:pos + 8 + chunk_len]
        if chunk_type == b"JSON":
            gltf = json.loads(chunk_data)
        elif chunk_type == b"BIN\x00":
            bin_chunk = chunk_data
        pos += 8 + chunk_len
    if gltf is None:
        raise GlbImportError("No JSON chunk found in .glb file.")
    return gltf, bin_chunk


def _read_accessor(gltf: dict, bin_buffer: bytes, accessor_index: int) -> list[tuple]:
    accessor = gltf["accessors"][accessor_index]
    if "bufferView" not in accessor:
        raise GlbImportError("Sparse/bufferView-less accessors aren't supported.")
    buffer_view = gltf["bufferViews"][accessor["bufferView"]]
    component_type = accessor["componentType"]
    count = accessor["count"]
    n = _TYPE_COMPONENT_COUNTS.get(accessor["type"])
    if n is None:
        raise GlbImportError(f"Unsupported accessor type: {accessor['type']!r}")
    fmt_char, comp_size = _COMPONENT_FORMATS.get(component_type, (None, None))
    if fmt_char is None:
        raise GlbImportError(f"Unsupported accessor componentType: {component_type}")
    offset = buffer_view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    default_stride = comp_size * n
    stride = buffer_view.get("byteStride", default_stride)
    fmt = f"<{n}{fmt_char}"
    out = []
    for i in range(count):
        base = offset + i * stride
        out.append(struct.unpack_from(fmt, bin_buffer, base))
    return out


def extract_primitives(gltf: dict, bin_buffer: bytes) -> list[Primitive]:
    """Walks glTF *nodes* (not just the raw meshes list) so collision-mesh
    nodes can be filtered out: swbf-unmunge's own glTF exporter (see
    model_builder.cpp) writes each modl segment's render geometry as one
    node, but if a 'coll' chunk was bundled alongside it (as
    export_model_bundle_to_file always does, to get a complete export) it
    *also* writes the collision BVH as extra node(s) named
    "collision_-<flags>-mesh<n>" - those aren't render geometry and must
    not be treated as a segment replacement candidate."""
    primitives = []
    for node in gltf.get("nodes", []):
        if "mesh" not in node:
            continue
        if str(node.get("name", "")).startswith("collision_-"):
            continue
        mesh = gltf["meshes"][node["mesh"]]
        for prim in mesh.get("primitives", []):
            if prim.get("mode", 4) != 4:  # 4 = TRIANGLES
                raise GlbImportError(
                    "Only triangle-list primitives are supported (this mesh uses a "
                    "different primitive mode - re-export with triangles, not strips/fans).")
            attrs = prim.get("attributes", {})
            if "POSITION" not in attrs:
                raise GlbImportError("A mesh primitive has no POSITION attribute.")
            positions = [v[:3] for v in _read_accessor(gltf, bin_buffer, attrs["POSITION"])]
            if "indices" in prim:
                indices = [v[0] for v in _read_accessor(gltf, bin_buffer, prim["indices"])]
            else:
                indices = list(range(len(positions)))
            if "NORMAL" in attrs:
                normals = [v[:3] for v in _read_accessor(gltf, bin_buffer, attrs["NORMAL"])]
            else:
                normals = _compute_flat_normals(positions, indices)
            if "TEXCOORD_0" in attrs:
                texcoords = [v[:2] for v in _read_accessor(gltf, bin_buffer, attrs["TEXCOORD_0"])]
            else:
                texcoords = [(0.0, 0.0)] * len(positions)
            material_name = ""
            if "material" in prim and prim["material"] < len(gltf.get("materials", [])):
                material_name = gltf["materials"][prim["material"]].get("name", "")
            primitives.append(Primitive(positions, normals, texcoords, indices,
                                        sanitize_name(material_name)))
    return primitives


def _compute_flat_normals(positions: list, indices: list) -> list:
    normals = [[0.0, 0.0, 0.0] for _ in positions]
    for i in range(0, len(indices) - 2, 3):
        ia, ib, ic = indices[i], indices[i + 1], indices[i + 2]
        ax, ay, az = positions[ia]
        bx, by, bz = positions[ib]
        cx, cy, cz = positions[ic]
        ux, uy, uz = bx - ax, by - ay, bz - az
        vx, vy, vz = cx - ax, cy - ay, cz - az
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        for idx in (ia, ib, ic):
            normals[idx][0] += nx
            normals[idx][1] += ny
            normals[idx][2] += nz
    out = []
    for nx, ny, nz in normals:
        length = (nx * nx + ny * ny + nz * nz) ** 0.5
        out.append((nx / length, ny / length, nz / length) if length > 1e-12 else (0.0, 1.0, 0.0))
    return out


# --- Binary encoding (verified format - see module docstring) --------------

_VBUF_FLAGS_POS_NORMAL_UV = 0x222  # position | normal | texcoords, all uncompressed
_TOPOLOGY_TRIANGLE_LIST = 4


def _build_ibuf(indices: list[int]) -> bytes:
    if len(indices) > 0xFFFF:
        raise GlbImportError(f"Too many indices ({len(indices)}) for a 16-bit index buffer.")
    return struct.pack(f"<I{len(indices)}H", len(indices), *indices)


def _build_vbuf(prim: Primitive) -> bytes:
    count = len(prim.positions)
    header = struct.pack("<III", count, 32, _VBUF_FLAGS_POS_NORMAL_UV)
    body = bytearray()
    for i in range(count):
        body += struct.pack("<3f", *prim.positions[i])
        body += struct.pack("<3f", *prim.normals[i])
        body += struct.pack("<2f", *prim.texcoords[i])
    return header + bytes(body)


def _build_segment_info(prim: Primitive) -> bytes:
    return struct.pack("<Iii", _TOPOLOGY_TRIANGLE_LIST, len(prim.positions), len(prim.indices) // 3)


def _bounding_box(all_positions: list) -> tuple:
    xs = [p[0] for p in all_positions]
    ys = [p[1] for p in all_positions]
    zs = [p[2] for p in all_positions]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def replace_segment_geometry(segm_payload: bytes, prim: Primitive) -> bytes:
    """Rebuilds one 'segm' chunk's payload: new INFO/IBUF/VBUF from `prim`,
    every other sub-chunk (MTRL, RTYP, TNAM, BNAM, ...) copied through
    unchanged and in its original relative position."""
    body = parse_body(segm_payload)
    new_info = tagged_block(b"INFO", _build_segment_info(prim))
    new_ibuf = tagged_block(b"IBUF", _build_ibuf(prim.indices))
    new_vbuf = tagged_block(b"VBUF", _build_vbuf(prim))
    parts = []
    vbuf_written = False
    for sub in body.chunks:
        if sub.tag == "INFO":
            parts.append(new_info)
        elif sub.tag == "IBUF":
            parts.append(new_ibuf)
        elif sub.tag == "VBUF":
            if not vbuf_written:
                parts.append(new_vbuf)
                vbuf_written = True
            # any additional original VBUF variants (e.g. a compressed one) are dropped
        else:
            parts.append(sub.raw_chunk_bytes() + sub.pad)
    if not vbuf_written:
        parts.append(new_vbuf)
    return body.prefix + b"".join(parts)


def replace_model_geometry(modl_payload: bytes, primitives: list[Primitive]) -> bytes:
    """Rebuilds a whole 'modl' chunk's payload, replacing each 'segm'
    child's geometry with the matching (by order) GLB primitive. Raises
    GlbCountMismatch if the counts don't match - silently pairing them up
    wrong would put the wrong material on the wrong geometry. A modl with NO
    segments (e.g. one made with 'Add New Chunk') is built from scratch
    instead - see _build_new_modl."""
    body = parse_body(modl_payload)
    segments = [sub for sub in body.chunks if sub.tag == "segm"]
    if not segments:
        return _build_new_modl(body, primitives)
    if any(ch.tag in ("BMAP", "SKIN") for sub in segments for ch in parse_body(sub.payload).chunks):
        raise GlbImportError(
            "This is a skinned character/vehicle model (its segments carry per-vertex bone data - "
            "SKIN/BMAP/VDAT). Replacing only its geometry would leave that bone data describing "
            "the old mesh and break it in-game. Right-click it and use "
            "'Import Character / Vehicle (.glb)...' instead.")
    if len(segments) != len(primitives):
        raise GlbCountMismatch(len(segments), len(primitives))

    all_positions = [p for prim in primitives for p in prim.positions]
    if not all_positions:
        raise GlbImportError("The .glb has no vertex data.")
    box_min, box_max = _bounding_box(all_positions)
    face_count = sum(len(prim.indices) // 3 for prim in primitives)

    parts = []
    seg_index = 0
    for sub in body.chunks:
        if sub.tag == "segm":
            new_payload = replace_segment_geometry(sub.payload, primitives[seg_index])
            new_chunk = Chunk("segm", new_payload)
            parts.append(new_chunk.raw_chunk_bytes() + (b"\x00" * _pad_len(len(new_chunk.payload))))
            seg_index += 1
        elif sub.tag == "INFO":
            parts.append(tagged_block(b"INFO", _rebuild_model_info(sub.payload, box_min, box_max, face_count)))
        else:
            parts.append(sub.raw_chunk_bytes() + sub.pad)
    return body.prefix + b"".join(parts)


def _build_new_modl(body: "Body", primitives: list[Primitive]) -> bytes:
    """Builds a whole modl from scratch, mirroring the layout of a real one
    (checked against rep.lvl's rep_weap_inf_rifle): NAME, NODE, INFO (68-byte
    swbf1-style form), one segm per primitive, SPHR. Each segm gets a plain
    'Normal' material (MTRL flags=1) whose texture (TNAM slot 0) is the
    .glb material's name, falling back to the model name - retexture by
    making a tex_ chunk with that name. NODE/BNAM name the model itself,
    which only renders if a 'skel' bone with that name exists in the same
    file (real models ship one); this doesn't create it."""
    name_chunk = next((s for s in body.chunks if s.tag == "NAME"), None)
    model_name = decode_ascii(name_chunk.payload) if name_chunk else "new_model"
    all_positions = [p for prim in primitives for p in prim.positions]
    if not all_positions:
        raise GlbImportError("The .glb has no vertex data.")
    box_min, box_max = _bounding_box(all_positions)
    face_count = sum(len(prim.indices) // 3 for prim in primitives)

    def z(text: str) -> bytes:
        return text.encode("ascii", "replace") + b"\x00"

    info = struct.pack("<3i", 0, 1, 0) + struct.pack("<3f3f3f3f", *box_min, *box_max, *box_min, *box_max)
    info += struct.pack("<II", 3, face_count)  # 3 = the same unknown int real models carry
    center = tuple((lo + hi) / 2 for lo, hi in zip(box_min, box_max))
    radius = max(sum((p[i] - center[i]) ** 2 for i in range(3)) ** 0.5 for p in all_positions)

    parts = [tagged_block(b"NAME", z(model_name)), tagged_block(b"NODE", z(model_name)),
             tagged_block(b"INFO", info)]
    for prim in primitives:
        segm = b"".join([
            tagged_block(b"INFO", _build_segment_info(prim)),
            tagged_block(b"MTRL", struct.pack("<I", 1)),
            tagged_block(b"RTYP", z("Normal")),
            tagged_block(b"TNAM", struct.pack("<I", 0) + z(prim.texture_name or model_name)),
            tagged_block(b"IBUF", _build_ibuf(prim.indices)),
            tagged_block(b"VBUF", _build_vbuf(prim)),
            tagged_block(b"BNAM", z(model_name)),
        ])
        parts.append(tagged_block(b"segm", segm))
    parts.append(tagged_block(b"SPHR", struct.pack("<4f", *center, radius)))
    return b"".join(parts)


def _rebuild_model_info(original_info: bytes, box_min: tuple, box_max: tuple, face_count: int) -> bytes:
    size = len(original_info)
    if size not in (68, 72):
        raise GlbImportError(f"Unrecognized modl INFO size ({size} bytes) - refusing to guess its layout.")
    leading = 12 if size == 68 else 16  # 3 or 4 leading int32s (swbf1 vs swbf2) - copied through
    out = bytearray(original_info)
    out[leading:leading + 48] = struct.pack("<3f3f3f3f", *box_min, *box_max, *box_min, *box_max)
    struct.pack_into("<I", out, size - 4, face_count)
    return bytes(out)


def import_glb_into_model(chunks: list["Chunk"], target: "Chunk", glb_bytes: bytes) -> bytes:
    """Given the full chunk list of the container `target` (a 'modl' chunk)
    lives in, and a .glb file's bytes, returns the new payload for `target`
    with its geometry replaced. `chunks` is accepted for symmetry with the
    export side (gather_model_bundle) even though this direction only
    touches `target` itself."""
    if target.tag != "modl":
        raise GlbImportError(f"GLB import only supports 'modl' chunks (got {target.tag!r}).")
    gltf, bin_buffer = parse_glb(glb_bytes)
    primitives = extract_primitives(gltf, bin_buffer)
    if not primitives:
        raise GlbImportError("The .glb file has no mesh primitives.")
    return replace_model_geometry(target.payload, primitives)


# ==================================================================================================
# bf1_skinned_import - merged in from bf1_skinned_import.py
# ==================================================================================================
# bf1_skinned_import.py - Replace a skinned character model (soldier bodies,
# anything whose 'modl' segments carry per-vertex bone indices) with new
# geometry, rebuilding every per-segment chunk in the same layout the game's
# own files use.
#
# FORMAT (verified against rep.lvl's rep_inf_trooper by decoding the original
# and re-encoding it byte-for-byte - see verify_roundtrip):
#
#   skel   INFO = owner model name \0 + u16 bone count; NAME / PRNT = that many
#          back-to-back \0-terminated bone / parent names; XFRM = that many
#          12-float parent-local transforms (3 basis columns, then translation).
#   modl   NAME, NODE (root bone), INFO (3 ints, vertex_box, visibility_box,
#          bone count, face count), [shdw...], segm..., SPHR.
#          vertex_box = bounds of the BONE-LOCAL vertex positions (it's the
#          range the compressed VBUF positions are quantized into);
#          visibility_box = bounds of the model in bind pose (world space).
#   segm   INFO (topology, vertex count, primitive count), MTRL, [MNAM], RTYP,
#          TNAM, IBUF, VBUF flags 0xf226 (compressed, stride 20), VBUF flags
#          0x226 (uncompressed, stride 36), VDAT (a 0x222 vertex buffer: no
#          bone field), SKIN (u32 count, u32 1, one byte per vertex), BMAP
#          (u32 count, one byte per entry).
#   vertex positions/normals are BONE-LOCAL: world = bind_world[bone] * local.
#   The per-vertex bone byte is an index into the segment's BMAP (a palette of
#   skeleton bone indices), and in both VBUFs it sits in bits 16-23 of a u32.
#
# Only hard skinning (one bone per vertex) is written - that's what the stock
# soldier models use (no bone_weights flag), so joints are rigid like the
# originals'.



VBUF_COMPRESSED = 0xF226
VBUF_SKINNED = 0x226
VBUF_PLAIN = 0x222
VBUF_TANGENTS = 0x40
TOPOLOGY_TRIANGLE_LIST = 4
TOPOLOGY_TRIANGLE_STRIP = 5


class SkinnedImportError(Exception):
    pass


# --- skeleton ---------------------------------------------------------------

@dataclass
class Bone:
    name: str
    parent: str
    transform: tuple  # 12 floats, parent-local


def _read_cstr(data: bytes, pos: int) -> tuple:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("ascii", "replace"), end + 1


def decode_skeleton_chunk(payload: bytes) -> tuple:
    body = parse_body(payload)
    kids = {s.tag: s for s in body.chunks}
    if not all(t in kids for t in ("INFO", "NAME", "PRNT", "XFRM")):
        raise SkinnedImportError("Malformed 'skel' chunk (missing INFO/NAME/PRNT/XFRM).")
    owner, pos = _read_cstr(kids["INFO"].payload, 0)
    count = struct.unpack_from("<H", kids["INFO"].payload, pos)[0]
    names, parents = [], []
    pos = 0
    for _ in range(count):
        n, pos = _read_cstr(kids["NAME"].payload, pos)
        names.append(n)
    pos = 0
    for _ in range(count):
        n, pos = _read_cstr(kids["PRNT"].payload, pos)
        parents.append(n)
    xfrm = kids["XFRM"].payload
    return owner, [Bone(names[i], parents[i], struct.unpack_from("<12f", xfrm, i * 48)) for i in range(count)]


def find_skeleton(sibling_chunks: list, model_name: str) -> Optional[list]:
    """A model's skeleton is a 'skel' chunk next to it whose INFO names it."""
    for ch in sibling_chunks:
        if ch.tag != "skel":
            continue
        try:
            owner, bones = decode_skeleton_chunk(ch.payload)
        except (SkinnedImportError, ValueError, struct.error):
            continue
        if owner == model_name:
            return bones
    return None


def _mat_from_transform(t) -> list:
    return [[t[0], t[3], t[6], t[9]], [t[1], t[4], t[7], t[10]],
            [t[2], t[5], t[8], t[11]], [0.0, 0.0, 0.0, 1.0]]


def mat_mul(a, b) -> list:
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def mat_point(m, p) -> tuple:
    x, y, z = p
    return (m[0][0] * x + m[0][1] * y + m[0][2] * z + m[0][3],
            m[1][0] * x + m[1][1] * y + m[1][2] * z + m[1][3],
            m[2][0] * x + m[2][1] * y + m[2][2] * z + m[2][3])


def mat_dir(m, v) -> tuple:
    x, y, z = v
    r = (m[0][0] * x + m[0][1] * y + m[0][2] * z,
         m[1][0] * x + m[1][1] * y + m[1][2] * z,
         m[2][0] * x + m[2][1] * y + m[2][2] * z)
    n = math.sqrt(r[0] ** 2 + r[1] ** 2 + r[2] ** 2)
    return (r[0] / n, r[1] / n, r[2] / n) if n > 1e-12 else (0.0, 1.0, 0.0)


def mat_inverse(m) -> list:
    a, b, cc = m[0][:3]
    d, e, f = m[1][:3]
    g, h, i = m[2][:3]
    det = a * (e * i - f * h) - b * (d * i - f * g) + cc * (d * h - e * g)
    if abs(det) < 1e-12:
        raise SkinnedImportError("Degenerate bone transform.")
    k = 1.0 / det
    r = [[(e * i - f * h) * k, (cc * h - b * i) * k, (b * f - cc * e) * k],
         [(f * g - d * i) * k, (a * i - cc * g) * k, (cc * d - a * f) * k],
         [(d * h - e * g) * k, (b * g - a * h) * k, (a * e - b * d) * k]]
    t = (m[0][3], m[1][3], m[2][3])
    ti = [-(r[j][0] * t[0] + r[j][1] * t[1] + r[j][2] * t[2]) for j in range(3)]
    return [r[0] + [ti[0]], r[1] + [ti[1]], r[2] + [ti[2]], [0.0, 0.0, 0.0, 1.0]]


_IDENTITY = [[1.0, 0.0, 0.0, 0.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0], [0.0, 0.0, 0.0, 1.0]]


def bind_world_matrices(bones: list, local_overrides: Optional[dict] = None) -> list:
    """World matrix per bone. `local_overrides` maps bone index -> a 4x4
    matrix to pre-multiply onto that bone's local transform (used to pose the
    skeleton to match a source mesh - see pose_arms_down)."""
    index = {b.name: i for i, b in enumerate(bones)}
    out = [None] * len(bones)

    def world(i):
        if out[i] is None:
            local = _mat_from_transform(bones[i].transform)
            if local_overrides and i in local_overrides:
                local = mat_mul(local, local_overrides[i])
            p = index.get(bones[i].parent)
            out[i] = mat_mul(world(p) if p is not None else _IDENTITY, local)
        return out[i]

    for i in range(len(bones)):
        world(i)
    return out


# --- segment decoding --------------------------------------------------------

@dataclass
class Vertex:
    pos: tuple      # bone-local
    normal: tuple   # bone-local
    uv: tuple
    bone: int       # index into the segment's BMAP


@dataclass
class Segment:
    children: list                  # [(tag, payload)] in file order
    topology: int
    indices: list
    vertices: list                  # [Vertex], from the uncompressed skinned VBUF
    bmap: list = field(default_factory=list)
    skinned: bool = True


def _strip_to_list(idx: list) -> list:
    tris = []
    for i in range(len(idx) - 2):
        a, b, cc = idx[i], idx[i + 1], idx[i + 2]
        if a == b or b == cc or a == cc:
            continue
        tris.extend((a, b, cc) if i % 2 == 0 else (b, a, cc))
    return tris


def decode_segment(payload: bytes) -> Segment:
    body = parse_body(payload)
    children = [(s.tag, s.payload) for s in body.chunks]
    info = next(p for t, p in children if t == "INFO")
    topology = struct.unpack_from("<I", info, 0)[0]
    ibuf = next(p for t, p in children if t == "IBUF")
    n = struct.unpack_from("<I", ibuf, 0)[0]
    indices = list(struct.unpack_from(f"<{n}H", ibuf, 4))
    bmap = []
    bm = next((p for t, p in children if t == "BMAP"), None)
    if bm is not None:
        bmap = list(bm[4:4 + struct.unpack_from("<I", bm, 0)[0]])
    vbufs = {struct.unpack_from("<I", p, 8)[0]: p for t, p in children if t == "VBUF"}
    verts = []
    if VBUF_SKINNED in vbufs:
        vb = vbufs[VBUF_SKINNED]
        count, stride, _ = struct.unpack_from("<III", vb, 0)
        for i in range(count):
            o = 12 + i * stride
            p = struct.unpack_from("<3f", vb, o)
            bone = (struct.unpack_from("<I", vb, o + 12)[0] >> 16) & 0xFF
            nn = struct.unpack_from("<3f", vb, o + 16)
            uv = struct.unpack_from("<2f", vb, o + 28)
            verts.append(Vertex(p, nn, uv, bone))
        return Segment(children, topology, indices, verts, bmap, True)
    if VBUF_PLAIN in vbufs:
        vb = vbufs[VBUF_PLAIN]
        count, stride, _ = struct.unpack_from("<III", vb, 0)
        for i in range(count):
            o = 12 + i * stride
            verts.append(Vertex(struct.unpack_from("<3f", vb, o), struct.unpack_from("<3f", vb, o + 12),
                                struct.unpack_from("<2f", vb, o + 24), 0))
    return Segment(children, topology, indices, verts, bmap, False)


def triangles(seg: Segment) -> list:
    if seg.topology == TOPOLOGY_TRIANGLE_STRIP:
        return _strip_to_list(seg.indices)
    return list(seg.indices[: len(seg.indices) // 3 * 3])


# --- encoders ----------------------------------------------------------------

def _q16(value: float, lo: float, hi: float) -> int:
    if hi - lo < 1e-12:
        return -32768
    q = round((value - lo) * 65535.0 / (hi - lo)) - 32768
    return max(-32768, min(32767, q))


def _wrap16(v: int) -> int:
    """int16 wraparound - matches the game's own quantizer, where UVs past +-16 wrap."""
    return (v + 32768) % 65536 - 32768


def _unorm8(v: float) -> int:
    return max(0, min(255, round((v + 1.0) * 0.5 * 255.0)))


def encode_vbuf_compressed(verts: list, vertex_box: tuple, normal_w: int = 0) -> bytes:
    lo, hi = vertex_box
    out = bytearray(struct.pack("<III", len(verts), 20, VBUF_COMPRESSED))
    for v in verts:
        out += struct.pack("<4h", *(_q16(v.pos[k], lo[k], hi[k]) for k in range(3)), 0)
        out += struct.pack("<I", (v.bone & 0xFF) << 16)
        nx, ny, nz = v.normal
        out += bytes((_unorm8(nz), _unorm8(ny), _unorm8(nx), normal_w))
        out += struct.pack("<2h", *(_wrap16(round(t * 2048.0)) for t in v.uv))
    return bytes(out)


VBUF_PLAIN_COMPRESSED = 0xD222


def encode_vbuf_plain_compressed(verts: list, vertex_box: tuple, normal_w: int = 0) -> bytes:
    lo, hi = vertex_box
    out = bytearray(struct.pack("<III", len(verts), 16, VBUF_PLAIN_COMPRESSED))
    for v in verts:
        out += struct.pack("<4h", *(_q16(v.pos[k], lo[k], hi[k]) for k in range(3)), 0)
        nx, ny, nz = v.normal
        out += bytes((_unorm8(nz), _unorm8(ny), _unorm8(nx), normal_w))
        out += struct.pack("<2h", *(_wrap16(round(t * 2048.0)) for t in v.uv))
    return bytes(out)


def encode_vbuf_skinned(verts: list) -> bytes:
    out = bytearray(struct.pack("<III", len(verts), 36, VBUF_SKINNED))
    for v in verts:
        out += struct.pack("<3fI3f2f", *v.pos, (v.bone & 0xFF) << 16, *v.normal, *v.uv)
    return bytes(out)


def encode_vbuf_plain(verts: list) -> bytes:
    out = bytearray(struct.pack("<III", len(verts), 32, VBUF_PLAIN))
    for v in verts:
        out += struct.pack("<3f3f2f", *v.pos, *v.normal, *v.uv)
    return bytes(out)


def encode_skin(verts: list) -> bytes:
    return struct.pack("<II", len(verts), 1) + bytes(v.bone & 0xFF for v in verts)


def encode_bmap(bmap: list) -> bytes:
    return struct.pack("<I", len(bmap)) + bytes(bmap)


def encode_ibuf(indices: list) -> bytes:
    if indices and max(indices) > 0xFFFF:
        raise SkinnedImportError("Segment has more than 65535 vertices.")
    return struct.pack(f"<I{len(indices)}H", len(indices), *indices)


def build_segment(template: Segment, verts: list, tri_indices: list, vertex_box: tuple) -> bytes:
    """Re-emit `template`'s children in their original order, regenerating
    every geometry-bearing chunk from `verts`/`tri_indices` (triangle list)
    and copying material chunks (MTRL/MNAM/RTYP/TNAM/BNAM/...) through."""
    normal_w = 0
    for t, p in template.children:
        if t == "VBUF" and len(p) >= 12 + 16:
            flags, stride = struct.unpack_from("<I", p, 8)[0], struct.unpack_from("<I", p, 4)[0]
            if flags == VBUF_COMPRESSED:
                normal_w = p[12 + 15]
            elif flags == VBUF_PLAIN_COMPRESSED:
                normal_w = p[12 + 11]
    parts = []
    written = set()
    for tag, payload in template.children:
        if tag == "INFO":
            new = struct.pack("<III", TOPOLOGY_TRIANGLE_LIST, len(verts), len(tri_indices) // 3)
        elif tag == "IBUF":
            new = encode_ibuf(tri_indices)
        elif tag == "VBUF":
            # tangent variants (bump-mapped segments: 0x266 / 0xf266) are
            # written as their plain equivalents - see rebuild_model
            flags = struct.unpack_from("<I", payload, 8)[0] & ~VBUF_TANGENTS
            if flags in written:
                continue
            written.add(flags)
            if flags == VBUF_COMPRESSED:
                new = encode_vbuf_compressed(verts, vertex_box, normal_w)
            elif flags == VBUF_SKINNED:
                new = encode_vbuf_skinned(verts)
            elif flags == VBUF_PLAIN:
                new = encode_vbuf_plain(verts)
            elif flags == VBUF_PLAIN_COMPRESSED:
                new = encode_vbuf_plain_compressed(verts, vertex_box, normal_w)
            else:
                raise SkinnedImportError(f"Template segment has an unsupported VBUF variant {flags:#x}.")
        elif tag == "VDAT":
            new = encode_vbuf_plain(verts)
        elif tag == "SKIN":
            new = encode_skin(verts)
        elif tag == "BMAP":
            new = encode_bmap(template.bmap)
        else:
            new = payload
        parts.append(tagged_block(tag.encode("ascii"), new))
    return b"".join(parts)


# --- source mesh ---------------------------------------------------------------

@dataclass
class Mesh:
    """World-space triangle mesh (triangle list). `groups` tags each vertex
    with the source texture (glTF image index, -1 = untextured) so the atlas
    builder and bone limits can tell hair from body etc."""
    positions: list
    normals: list
    uvs: list
    tris: list
    groups: list = field(default_factory=list)


def _node_matrix(node: dict) -> list:
    if "matrix" in node:
        m = node["matrix"]  # column-major
        return [[m[0], m[4], m[8], m[12]], [m[1], m[5], m[9], m[13]],
                [m[2], m[6], m[10], m[14]], [0.0, 0.0, 0.0, 1.0]]
    tx, ty, tz = node.get("translation", (0.0, 0.0, 0.0))
    x, y, z, w = node.get("rotation", (0.0, 0.0, 0.0, 1.0))
    sx, sy, sz = node.get("scale", (1.0, 1.0, 1.0))
    r = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[r[0][0] * sx, r[0][1] * sy, r[0][2] * sz, tx],
            [r[1][0] * sx, r[1][1] * sy, r[1][2] * sz, ty],
            [r[2][0] * sx, r[2][1] * sy, r[2][2] * sz, tz],
            [0.0, 0.0, 0.0, 1.0]]


def load_glb_mesh(glb_bytes: bytes) -> Mesh:
    """Every triangle primitive in the file's scene, node transforms baked
    in, merged into one mesh (collision_- nodes skipped, like the static
    importer)."""
    gltf, binbuf = parse_glb(glb_bytes)
    nodes = gltf.get("nodes", [])
    children = {ci for n in nodes for ci in n.get("children", [])}
    mesh = Mesh([], [], [], [], [])

    def image_of(prim):
        mats = gltf.get("materials", [])
        if "material" not in prim or prim["material"] >= len(mats):
            return -1
        mat = mats[prim["material"]]
        tex = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index")
        if tex is None:  # older exporters: the colour map lives in the spec/gloss extension
            tex = (mat.get("extensions", {}).get("KHR_materials_pbrSpecularGlossiness", {})
                   .get("diffuseTexture", {}).get("index"))
        if tex is None or tex >= len(gltf.get("textures", [])):
            return -1
        return gltf["textures"][tex].get("source", -1)

    scene = gltf.get("scenes", [{}])[gltf.get("scene", 0)] if gltf.get("scenes") else {}
    roots = scene.get("nodes") or [i for i in range(len(nodes)) if i not in children]
    worlds, order = {}, []

    def walk(ni, parent):
        worlds[ni] = mat_mul(parent, _node_matrix(nodes[ni]))
        order.append(ni)
        for ci in nodes[ni].get("children", []):
            walk(ci, worlds[ni])

    for ri in roots:
        walk(ri, _IDENTITY)

    def skin_matrices(skin_index):
        """Per joint: joint world matrix * inverse bind matrix - what a glTF
        viewer uses to place a rigged mesh (the mesh node's own transform
        is ignored for those, per the spec)."""
        skin = gltf["skins"][skin_index]
        joints = skin.get("joints", [])
        if "inverseBindMatrices" in skin:
            ibms = _read_accessor(gltf, binbuf, skin["inverseBindMatrices"])
        else:
            ibms = [(1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1)] * len(joints)
        out = []
        for j, m in zip(joints, ibms):  # accessor data is column-major
            ibm = [[m[0], m[4], m[8], m[12]], [m[1], m[5], m[9], m[13]],
                   [m[2], m[6], m[10], m[14]], [0.0, 0.0, 0.0, 1.0]]
            out.append(mat_mul(worlds.get(j, _IDENTITY), ibm))
        return out

    for ni in order:
        node = nodes[ni]
        if "mesh" not in node or str(node.get("name", "")).startswith("collision_-"):
            continue
        world = worlds[ni]
        joint_mats = skin_matrices(node["skin"]) if "skin" in node and gltf.get("skins") else None
        for prim in gltf["meshes"][node["mesh"]].get("primitives", []):
            if prim.get("mode", 4) != 4:
                raise SkinnedImportError("Only triangle-list primitives are supported.")
            attrs = prim["attributes"]
            pos = [v[:3] for v in _read_accessor(gltf, binbuf, attrs["POSITION"])]
            idx = ([v[0] for v in _read_accessor(gltf, binbuf, prim["indices"])]
                   if "indices" in prim else list(range(len(pos))))
            nrm = ([v[:3] for v in _read_accessor(gltf, binbuf, attrs["NORMAL"])]
                   if "NORMAL" in attrs else _compute_flat_normals(pos, idx))
            uv = ([v[:2] for v in _read_accessor(gltf, binbuf, attrs["TEXCOORD_0"])]
                  if "TEXCOORD_0" in attrs else [(0.0, 0.0)] * len(pos))
            base = len(mesh.positions)
            if joint_mats is not None and "JOINTS_0" in attrs and "WEIGHTS_0" in attrs:
                joints = _read_accessor(gltf, binbuf, attrs["JOINTS_0"])
                weights = _read_accessor(gltf, binbuf, attrs["WEIGHTS_0"])
                wscale = {5121: 255.0, 5123: 65535.0}.get(gltf["accessors"][attrs["WEIGHTS_0"]]["componentType"], 1.0)
                for p, n, js, ws in zip(pos, nrm, joints, weights):
                    acc_p, acc_n, total = [0.0, 0.0, 0.0], [0.0, 0.0, 0.0], 0.0
                    for j, w in zip(js, ws):
                        w = w / wscale
                        if w <= 0 or j >= len(joint_mats):
                            continue
                        m = joint_mats[j]
                        q = mat_point(m, p)
                        r = (m[0][0] * n[0] + m[0][1] * n[1] + m[0][2] * n[2],
                             m[1][0] * n[0] + m[1][1] * n[1] + m[1][2] * n[2],
                             m[2][0] * n[0] + m[2][1] * n[1] + m[2][2] * n[2])
                        for k in range(3):
                            acc_p[k] += w * q[k]
                            acc_n[k] += w * r[k]
                        total += w
                    if total > 0:
                        mesh.positions.append(tuple(v / total for v in acc_p))
                        ln = math.sqrt(sum(v * v for v in acc_n)) or 1.0
                        mesh.normals.append(tuple(v / ln for v in acc_n))
                    else:
                        mesh.positions.append(mat_point(world, p))
                        mesh.normals.append(mat_dir(world, n))
            else:
                mesh.positions += [mat_point(world, p) for p in pos]
                mesh.normals += [mat_dir(world, n) for n in nrm]
            mesh.uvs += [tuple(t) for t in uv]
            mesh.groups += [image_of(prim)] * len(pos)
            mesh.tris += [base + i for i in idx[: len(idx) // 3 * 3]]
    if not mesh.tris:
        raise SkinnedImportError("The .glb has no triangles.")
    return mesh


def fit_to_height(mesh: Mesh, height: float) -> Mesh:
    """Uniform scale so the mesh is `height` tall, feet on y=0, centered on x/z."""
    ys = [p[1] for p in mesh.positions]
    xs = [p[0] for p in mesh.positions]
    zs = [p[2] for p in mesh.positions]
    s = height / (max(ys) - min(ys))
    cx, cz, y0 = (min(xs) + max(xs)) / 2, (min(zs) + max(zs)) / 2, min(ys)
    pos = [((x - cx) * s, (y - y0) * s, (z - cz) * s) for x, y, z in mesh.positions]
    return Mesh(pos, list(mesh.normals), list(mesh.uvs), list(mesh.tris), list(mesh.groups))


FIT_AXES = {"Height": 1, "Length (front-back)": 2, "Width": 0, "Largest side": None}


def rotation_matrix(turn_x: float = 0.0, turn_y: float = 0.0, turn_z: float = 0.0) -> list:
    """3x3 rotation: turn about X (tips it forward/back - flips an
    upside-down model with 180), then Y (spins it to face another way), then
    Z (rolls it onto its side), in degrees."""
    def rx(a):
        c_, s = math.cos(a), math.sin(a)
        return [[1, 0, 0], [0, c_, -s], [0, s, c_]]

    def ry(a):
        c_, s = math.cos(a), math.sin(a)
        return [[c_, 0, s], [0, 1, 0], [-s, 0, c_]]

    def rz(a):
        c_, s = math.cos(a), math.sin(a)
        return [[c_, -s, 0], [s, c_, 0], [0, 0, 1]]

    def mul(a, b):
        return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)] for i in range(3)]

    return mul(rz(math.radians(turn_z)), mul(ry(math.radians(turn_y)), rx(math.radians(turn_x))))


def fit_to_bounds(mesh: Mesh, lo: tuple, hi: tuple, axis: str = "Height", size: Optional[float] = None,
                  rotate_deg=0.0) -> Mesh:
    """Rotate the mesh (`rotate_deg` = degrees about the vertical axis, or an
    (x, y, z) tuple of turns - see rotation_matrix), scale it uniformly so
    its extent along `axis` (a FIT_AXES key) is `size` (default: the target
    box's own extent on that axis), then place it in the target box
    `lo`/`hi`: centred on x/z, bottom on the box's floor. For a character
    that's "trooper height, feet on the ground"; for a vehicle, "same
    length, same place" as the model it replaces."""
    turns = tuple(rotate_deg) if isinstance(rotate_deg, (tuple, list)) else (0.0, float(rotate_deg), 0.0)
    r = rotation_matrix(*turns)

    def rot(v):
        return (r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2])

    pos = [rot(p) for p in mesh.positions]
    nrm = [rot(n) for n in mesh.normals]
    mlo = [min(p[k] for p in pos) for k in range(3)]
    mhi = [max(p[k] for p in pos) for k in range(3)]
    ext = [mhi[k] - mlo[k] for k in range(3)]
    k_axis = FIT_AXES[axis]
    if k_axis is None:
        k_axis = max(range(3), key=lambda k: ext[k])
        target_k = max(range(3), key=lambda k: hi[k] - lo[k])
    else:
        target_k = k_axis
    if size is None:
        size = hi[target_k] - lo[target_k]
    s = size / (ext[k_axis] or 1.0)
    cx, cz = (mlo[0] + mhi[0]) / 2, (mlo[2] + mhi[2]) / 2
    tx, tz = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2
    out = [((x - cx) * s + tx, (y - mlo[1]) * s + lo[1], (z - cz) * s + tz) for x, y, z in pos]
    return Mesh(out, nrm, list(mesh.uvs), list(mesh.tris), list(mesh.groups))


def template_bounds(template_payload: bytes, bones: list) -> tuple:
    """Actual bind-pose extents (lo, hi) of a model's own geometry - tighter
    than its INFO visibility box, which for some vehicles is a loose cube."""
    body = parse_body(template_payload)
    mats = bind_world_matrices(bones)
    pts = []
    for s in body.chunks:
        if s.tag == "segm":
            seg = decode_segment(s.payload)
            if seg.skinned and seg.bmap:
                pts += [mat_point(mats[seg.bmap[v.bone]], v.pos) for v in seg.vertices if v.bone < len(seg.bmap)]
    if not pts:
        raise SkinnedImportError("The model being replaced has no skinned geometry to measure.")
    return (tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3)))


def is_character(bones: list) -> bool:
    """Humanoid skeleton (has arms) vs vehicle/other."""
    return _find_bone(bones, "bone_l_upperarm") is not None and _find_bone(bones, "bone_r_upperarm") is not None


def load_glb_images(glb_bytes: bytes) -> dict:
    """{glTF image index: PIL RGBA image} for every image embedded in the .glb."""
    import io
    from PIL import Image
    gltf, binbuf = parse_glb(glb_bytes)
    out = {}
    for i, im in enumerate(gltf.get("images", [])):
        if "bufferView" not in im:
            continue
        bv = gltf["bufferViews"][im["bufferView"]]
        start = bv.get("byteOffset", 0)
        out[i] = Image.open(io.BytesIO(binbuf[start:start + bv["byteLength"]])).convert("RGBA")
    return out


def build_atlas(mesh: Mesh, images: dict, layout: dict, size: int = 512, gutter: float = 2.0) -> tuple:
    """Packs several source textures into one `size`x`size` image (the game
    gives a character ONE texture - e.g. a class's OverrideTexture - so a
    multi-texture model has to share an atlas). `layout` = {image index:
    (x, y, w, h) in atlas pixels}. UVs are remapped into each image's
    region; tiled UVs (outside 0..1) are wrapped per triangle, splitting
    vertices where neighbouring triangles wrap differently. Transparent
    pixels are filled with the image's average colour (the stock soldier
    material is opaque). Returns (new mesh, atlas RGBA bytes)."""
    from PIL import Image
    atlas = Image.new("RGBA", (size, size), (128, 128, 128, 255))
    for img_i, (x, y, w, h) in layout.items():
        src = images[img_i]
        opaque = [p for p in src.resize((64, 64)).getdata() if p[3] > 128] or [(128, 128, 128, 255)]
        avg = tuple(sum(p[k] for p in opaque) // len(opaque) for k in range(3)) + (255,)
        flat = Image.new("RGBA", src.size, avg)
        flat.alpha_composite(src)
        atlas.paste(flat.resize((w, h), Image.LANCZOS), (x, y))
    positions, normals, uvs, groups, tris, remap = [], [], [], [], [], {}
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        g = mesh.groups[tri[0]] if mesh.groups else -1
        su = math.floor(sum(mesh.uvs[v][0] for v in tri) / 3)
        sv = math.floor(sum(mesh.uvs[v][1] for v in tri) / 3)
        for v in tri:
            key = (v, su, sv)
            if key not in remap:
                remap[key] = len(positions)
                u, vv = mesh.uvs[v]
                u = min(1.0, max(0.0, u - su))
                vv = min(1.0, max(0.0, vv - sv))
                if g in layout:
                    x, y, w, h = layout[g]
                    u = (x + gutter + u * (w - 2 * gutter)) / size
                    vv = (y + gutter + vv * (h - 2 * gutter)) / size
                positions.append(mesh.positions[v])
                normals.append(mesh.normals[v])
                uvs.append((u, vv))
                groups.append(g)
            tris.append(remap[key])
    return Mesh(positions, normals, uvs, tris, groups), atlas.tobytes()


# --- decimation (quadric error metric, half-edge collapse) --------------------

def _plane_quadric(a, b, cc, d, w):
    return [w * a * a, w * a * b, w * a * cc, w * a * d, w * b * b, w * b * cc, w * b * d,
            w * cc * cc, w * cc * d, w * d * d]


def _q_err(q, p):
    x, y, z = p
    return (q[0] * x * x + 2 * q[1] * x * y + 2 * q[2] * x * z + 2 * q[3] * x + q[4] * y * y
            + 2 * q[5] * y * z + 2 * q[6] * y + q[7] * z * z + 2 * q[8] * z + q[9])


def _face_normal(a, b, cc):
    ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    vx, vy, vz = cc[0] - a[0], cc[1] - a[1], cc[2] - a[2]
    return (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)


PRE_REDUCE_LIMIT = 40000


def pre_reduce(mesh: Mesh, max_vertices: int, locked: set = frozenset()) -> tuple:
    """Fast first pass for very heavy meshes (a 380k-vertex tank would take
    the quadric simplifier minutes): snaps vertices to a grid fine enough to
    land under `max_vertices`, merging ones in the same cell with the same
    texture and roughly the same UV (so texture seams survive). Locked
    vertices are never merged. Returns (mesh, locked indices in the new mesh)."""
    P = mesh.positions
    lo = [min(p[k] for p in P) for k in range(3)]
    hi = [max(p[k] for p in P) for k in range(3)]
    size = max(hi[k] - lo[k] for k in range(3)) or 1.0
    groups = mesh.groups or [0] * len(P)
    for cells in (1024, 768, 512, 384, 256, 192, 128, 96, 64, 48, 32):
        cell = size / cells
        keys = {}
        remap = []
        for i, p in enumerate(P):
            if i in locked:
                key = ("locked", i)
            else:
                uv = mesh.uvs[i]
                key = (int((p[0] - lo[0]) / cell), int((p[1] - lo[1]) / cell), int((p[2] - lo[2]) / cell),
                       groups[i], round(uv[0] * 16), round(uv[1] * 16))
            j = keys.get(key)
            if j is None:
                j = keys[key] = len(keys)
            remap.append(j)
        if len(keys) <= max_vertices:
            break
    count = len(keys)
    first = [-1] * count
    for i, j in enumerate(remap):
        if first[j] < 0:
            first[j] = i
    positions = [P[i] for i in first]
    normals = [mesh.normals[i] for i in first]
    uvs = [mesh.uvs[i] for i in first]
    new_groups = [groups[i] for i in first]
    tris, seen = [], set()
    for t in range(0, len(mesh.tris), 3):
        a, b, cc = (remap[v] for v in mesh.tris[t:t + 3])
        if a == b or b == cc or a == cc:
            continue
        key = tuple(sorted((a, b, cc)))
        if key in seen:
            continue
        seen.add(key)
        tris += (a, b, cc)
    new_locked = {remap[i] for i in locked}
    return Mesh(positions, normals, uvs, tris, new_groups), new_locked


def decimate(mesh: Mesh, target_vertices: int, boundary_weight: float = 50.0,
             locked: Optional[set] = None) -> Mesh:
    """Garland-Heckbert quadric simplification with half-edge collapses (a
    vertex merges into a neighbour and takes its position, UV and normal -
    so UVs never get interpolated across texture seams). Open edges (mesh
    borders and UV seams) get heavy perpendicular-plane quadrics so they
    hold their shape, and collapses that would flip a triangle are
    rejected. `locked` vertices never move or merge away, so any triangle
    made only of locked vertices survives untouched (protected areas) - if
    they alone exceed the budget, the result simply ends up over it."""
    if len(mesh.positions) <= target_vertices:
        return mesh
    locked = locked or set()
    if len(mesh.positions) > PRE_REDUCE_LIMIT and len(locked) < PRE_REDUCE_LIMIT // 2:
        mesh, locked = pre_reduce(mesh, PRE_REDUCE_LIMIT, locked)
        if len(mesh.positions) <= target_vertices:
            return mesh
    pos = mesh.positions
    n = len(pos)
    faces = [list(mesh.tris[i:i + 3]) for i in range(0, len(mesh.tris), 3)]
    vfaces = [set() for _ in range(n)]
    Q = [[0.0] * 10 for _ in range(n)]
    edge_count = {}
    for fi, (a, b, cc) in enumerate(faces):
        for v in (a, b, cc):
            vfaces[v].add(fi)
        nx, ny, nz = _face_normal(pos[a], pos[b], pos[cc])
        ln = math.sqrt(nx * nx + ny * ny + nz * nz)
        if ln < 1e-15:
            continue
        nx, ny, nz = nx / ln, ny / ln, nz / ln
        d = -(nx * pos[a][0] + ny * pos[a][1] + nz * pos[a][2])
        q = _plane_quadric(nx, ny, nz, d, ln * 0.5)
        for v in (a, b, cc):
            Qv = Q[v]
            for k in range(10):
                Qv[k] += q[k]
        for e in ((a, b), (b, cc), (cc, a)):
            key = (min(e), max(e))
            edge_count[key] = edge_count.get(key, 0) + 1
    for (a, b), cnt in edge_count.items():
        if cnt != 1:
            continue
        fi = next(iter(vfaces[a] & vfaces[b]))
        fa, fb, fc = faces[fi]
        fn = _face_normal(pos[fa], pos[fb], pos[fc])
        ex, ey, ez = (pos[b][k] - pos[a][k] for k in range(3))
        px, py, pz = ey * fn[2] - ez * fn[1], ez * fn[0] - ex * fn[2], ex * fn[1] - ey * fn[0]
        ln = math.sqrt(px * px + py * py + pz * pz)
        if ln < 1e-15:
            continue
        px, py, pz = px / ln, py / ln, pz / ln
        d = -(px * pos[a][0] + py * pos[a][1] + pz * pos[a][2])
        q = _plane_quadric(px, py, pz, d, boundary_weight * (ex * ex + ey * ey + ez * ez))
        for v in (a, b):
            for k in range(10):
                Q[v][k] += q[k]

    neighbors = [set() for _ in range(n)]
    for a, b in edge_count:
        neighbors[a].add(b)
        neighbors[b].add(a)
    version = [0] * n
    alive = [bool(vf) for vf in vfaces]
    heap = []

    def push(u, v):
        if u in locked:
            return
        qs = [Q[u][k] + Q[v][k] for k in range(10)]
        heapq.heappush(heap, (_q_err(qs, pos[v]), u, v, version[u], version[v]))

    for a, b in edge_count:
        push(a, b)
        push(b, a)

    live = sum(alive)
    live_faces = [True] * len(faces)
    while live > target_vertices and heap:
        cost, u, v, vu, vv = heapq.heappop(heap)
        if not (alive[u] and alive[v]) or version[u] != vu or version[v] != vv or v not in neighbors[u]:
            continue
        # reject if moving u onto v flips any surviving face of u
        flips = False
        for fi in vfaces[u]:
            f = faces[fi]
            if v in f:
                continue
            old = _face_normal(*(pos[x] for x in f))
            new = _face_normal(*(pos[v] if x == u else pos[x] for x in f))
            if old[0] * new[0] + old[1] * new[1] + old[2] * new[2] <= 0.0:
                flips = True
                break
        if flips:
            continue
        touched = {v}
        for fi in list(vfaces[u]):
            f = faces[fi]
            if v in f:
                live_faces[fi] = False
                for x in f:
                    if x != u:
                        vfaces[x].discard(fi)
                        touched.add(x)
            else:
                f[f.index(u)] = v
                vfaces[v].add(fi)
        vfaces[u] = set()
        for w in neighbors[u]:
            if w != v:
                neighbors[w].discard(u)
                neighbors[w].add(v)
                neighbors[v].add(w)
        neighbors[v].discard(u)
        neighbors[u] = set()
        alive[u] = False
        for k in range(10):
            Q[v][k] += Q[u][k]
        version[v] += 1
        live -= 1
        for x in touched:  # vertices left with no triangles no longer count
            if alive[x] and not vfaces[x]:
                alive[x] = False
                live -= 1
                for w in neighbors[x]:
                    neighbors[w].discard(x)
                neighbors[x] = set()
        for w in neighbors[v]:
            push(v, w)
            push(w, v)

    remap, positions, normals, uvs, groups = {}, [], [], [], []
    tris = []
    for fi, f in enumerate(faces):
        if not live_faces[fi] or len(set(f)) < 3:
            continue
        for x in f:
            if x not in remap:
                remap[x] = len(positions)
                positions.append(pos[x])
                normals.append(mesh.normals[x])
                uvs.append(mesh.uvs[x])
                groups.append(mesh.groups[x] if mesh.groups else -1)
            tris.append(remap[x])
    return Mesh(positions, normals, uvs, tris, groups)


# --- skeleton fitting and bone assignment ---------------------------------------

def _subtree(bones: list, root: int) -> list:
    out, stack = [], [root]
    while stack:
        i = stack.pop()
        out.append(i)
        stack.extend(j for j, b in enumerate(bones) if b.parent == bones[i].name)
    return out


def _rot_about(pivot, axis, deg) -> list:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    x, y, z = axis
    r = [[ca + x * x * (1 - ca), x * y * (1 - ca) - z * sa, x * z * (1 - ca) + y * sa],
         [y * x * (1 - ca) + z * sa, ca + y * y * (1 - ca), y * z * (1 - ca) - x * sa],
         [z * x * (1 - ca) - y * sa, z * y * (1 - ca) + x * sa, ca + z * z * (1 - ca)]]
    px, py, pz = pivot
    t = [px - (r[0][0] * px + r[0][1] * py + r[0][2] * pz),
         py - (r[1][0] * px + r[1][1] * py + r[1][2] * pz),
         pz - (r[2][0] * px + r[2][1] * py + r[2][2] * pz)]
    return [r[0] + [t[0]], r[1] + [t[1]], r[2] + [t[2]], [0.0, 0.0, 0.0, 1.0]]


def _find_bone(bones: list, *suffixes) -> Optional[int]:
    for i, b in enumerate(bones):
        if b.name.lower() in suffixes:
            return i
    return None


def estimate_arm_drop(mesh: Mesh, bones: list, mats: list) -> float:
    """Degrees the source mesh's arms hang below the skeleton's arms (0 for a
    T-pose source, ~45 for an A-pose one). Measured from the shoulder joint to
    the source's outermost points, per side, averaged."""
    drops = []
    for side in ("l", "r"):
        ua = _find_bone(bones, f"bone_{side}_upperarm")
        tip = _find_bone(bones, f"bone_{side}_hand") or _find_bone(bones, f"bone_{side}_forearm")
        if ua is None or tip is None:
            continue
        sx, sy = mats[ua][0][3], mats[ua][1][3]
        tx, ty = mats[tip][0][3], mats[tip][1][3]
        sign = 1.0 if tx > sx else -1.0
        pts = [p for p in mesh.positions if (p[0] - sx) * sign > 0.1 and p[1] > sy - 0.9]
        if len(pts) < 10:
            continue
        pts.sort(key=lambda p: -(p[0] - sx) * sign)
        tipset = pts[: max(5, len(pts) // 50)]
        mx = sum(p[0] for p in tipset) / len(tipset)
        my = sum(p[1] for p in tipset) / len(tipset)
        src = math.degrees(math.atan2(my - sy, abs(mx - sx)))
        skel = math.degrees(math.atan2(ty - sy, abs(tx - sx)))
        drops.append(skel - src)
    return sum(drops) / len(drops) if drops else 0.0


def pose_arms(bones: list, mats: list, drop_deg: float) -> list:
    """Copy of `mats` with each arm (upperarm subtree) rotated down by
    `drop_deg` around the shoulder, in the body's front plane."""
    out = [list(map(list, m)) for m in mats]
    if abs(drop_deg) < 1e-6:
        return out
    for side in ("l", "r"):
        ua = _find_bone(bones, f"bone_{side}_upperarm")
        if ua is None:
            continue
        pivot = (mats[ua][0][3], mats[ua][1][3], mats[ua][2][3])
        child = next((j for j, b in enumerate(bones) if b.parent == bones[ua].name), None)
        outward = 1.0 if child is None or mats[child][0][3] >= pivot[0] else -1.0
        r = _rot_about(pivot, (0.0, 0.0, 1.0), -drop_deg * outward)
        for i in _subtree(bones, ua):
            out[i] = mat_mul(r, mats[i])
    return out


def template_skin_points(template_payload: bytes, mats: list) -> list:
    """[(world position, skeleton bone index)] for every skinned vertex of a
    model, placed with `mats` (bind or posed world matrices)."""
    body = parse_body(template_payload)
    out = []
    for s in body.chunks:
        if s.tag != "segm":
            continue
        seg = decode_segment(s.payload)
        if seg.skinned and seg.bmap:
            out += [(mat_point(mats[seg.bmap[v.bone]], v.pos), seg.bmap[v.bone]) for v in seg.vertices]
    return out


def _shell(r: int):
    """Grid offsets at exactly Chebyshev distance r (the surface of a cube)."""
    if r == 0:
        yield (0, 0, 0)
        return
    for dx in range(-r, r + 1):
        for dy in range(-r, r + 1):
            if abs(dx) == r or abs(dy) == r:
                for dz in range(-r, r + 1):
                    yield (dx, dy, dz)
            else:
                yield (dx, dy, -r)
                yield (dx, dy, r)


def transfer_bones(points: list, skin_points: list) -> list:
    """Skin transfer: each point takes the bone of the nearest template vertex
    (the original model's own artist-made skinning). Uses scipy's KD-tree or
    numpy when installed (milliseconds), otherwise a pure-Python grid search."""
    if not skin_points:
        raise SkinnedImportError("The model being replaced has no skinned vertices to copy bones from.")
    if not points:
        return []
    try:
        import numpy as np
    except ImportError:
        return _transfer_bones_grid(points, skin_points)
    ref = np.asarray([p for p, _ in skin_points], dtype=np.float64)
    bones = np.asarray([b for _, b in skin_points])
    query = np.asarray(points, dtype=np.float64)
    try:
        from scipy.spatial import cKDTree
        nearest = cKDTree(ref).query(query)[1]
    except ImportError:
        nearest = np.empty(len(query), dtype=np.int64)
        for start in range(0, len(query), 512):  # chunked so memory stays small
            chunk = query[start:start + 512]
            d = ((chunk[:, None, :] - ref[None, :, :]) ** 2).sum(axis=2)
            nearest[start:start + 512] = d.argmin(axis=1)
    return [int(b) for b in bones[nearest]]


def _transfer_bones_grid(points: list, skin_points: list) -> list:
    """Pure-Python fallback for transfer_bones: a grid sized to the template
    (~40 cells across, so it works the same for a 2m soldier and a 15m
    walker), searched in growing shells that stop as soon as no nearer point
    can exist."""
    lo = [min(p[k] for p, _ in skin_points) for k in range(3)]
    hi = [max(p[k] for p, _ in skin_points) for k in range(3)]
    cell = max(max(hi[k] - lo[k] for k in range(3)) / 40.0, 1e-4)
    grid = {}
    for p, b in skin_points:
        grid.setdefault((int(p[0] // cell), int(p[1] // cell), int(p[2] // cell)), []).append((p, b))
    keys = list(grid)
    kmin = [min(k[i] for k in keys) for i in range(3)]
    kmax = [max(k[i] for k in keys) for i in range(3)]
    out = []
    for p in points:
        g = (int(p[0] // cell), int(p[1] // cell), int(p[2] // cell))
        # no point in searching further out than the far corner of the occupied grid
        r_max = max(max(abs(g[i] - kmin[i]), abs(g[i] - kmax[i])) for i in range(3))
        best, bd, r = None, None, 0
        while r <= r_max:
            if bd is not None and ((r - 1) * cell) ** 2 > bd:
                break  # everything in this shell and beyond is farther than the best so far
            for dx, dy, dz in _shell(r):
                for q, b in grid.get((g[0] + dx, g[1] + dy, g[2] + dz), ()):
                    d = (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2
                    if bd is None or d < bd:
                        best, bd = b, d
            r += 1
        out.append(best)
    return out


# --- model rebuild ----------------------------------------------------------------

# Per-SEGMENT ceilings, found by testing in-game: a soldier LOD segment with
# 7,500 vertices / 29,868 indices animates fine; 7,900 / 31,917 animates
# glitchily and the soldier's weapon disappears (while the same model split
# into two smaller segments looked fine). So instead of capping how detailed
# a model can be, big segments are split into several copies of the
# template segment (same material, texture and bone palette), each under
# these limits.
SEGMENT_VERTEX_LIMIT = 7000
SEGMENT_INDEX_LIMIT = 28000
# The model header's triangle count drives the game's detail level: in-game,
# 9,956 there is fine and 10,639 puts the unit in its far-away mode (weapon
# hidden, minimal animation). Capped independently of the model being
# replaced, which may itself be an earlier import with a too-high count.
SAFE_HEADER_FACES = 9000


def _template_key(seg: Segment) -> tuple:
    """Material + bone palette of a segment - two segments with the same key
    are copies made by an earlier import's split (see _split_triangles)."""
    return (tuple(seg.bmap), tuple((t, p) for t, p in seg.children
                                   if t in ("MTRL", "MNAM", "RTYP", "TNAM", "BNAM")))


def _split_triangles(mesh: Mesh, tris: list) -> list:
    """Splits a segment's triangles into spatially compact pieces that each
    stay under SEGMENT_VERTEX_LIMIT vertices and SEGMENT_INDEX_LIMIT indices
    (triangles sorted along the segment's longest axis, then filled greedily)."""
    if not tris:
        return []
    unique = {v for tri in tris for v in tri}
    if len(unique) <= SEGMENT_VERTEX_LIMIT and len(tris) * 3 <= SEGMENT_INDEX_LIMIT:
        return [tris]
    P = mesh.positions
    cents = [tuple(sum(P[v][k] for v in tri) / 3 for k in range(3)) for tri in tris]
    axis = max(range(3), key=lambda k: max(ct[k] for ct in cents) - min(ct[k] for ct in cents))
    order = sorted(range(len(tris)), key=lambda i: cents[i][axis])
    pieces, current, seen = [], [], set()
    for i in order:
        new = [v for v in tris[i] if v not in seen]
        if current and (len(seen) + len(new) > SEGMENT_VERTEX_LIMIT or (len(current) + 1) * 3 > SEGMENT_INDEX_LIMIT):
            pieces.append(current)
            current, seen = [], set()
            new = list(tris[i])
        current.append(tris[i])
        seen.update(new)
    if current:
        pieces.append(current)
    return pieces


_SPECIAL_MATERIAL_FLAGS = 0x4 | 0x80 | 0x100  # swbf1 transparent | additive | glow


def _is_opaque(seg: Segment) -> bool:
    mtrl = next((p for t, p in seg.children if t == "MTRL"), None)
    return mtrl is None or not (struct.unpack_from("<I", mtrl, 0)[0] & _SPECIAL_MATERIAL_FLAGS)


def rebuild_model(template_payload: bytes, bones: list, mesh: Mesh, posed: list,
                  bone_limits: Optional[dict] = None, texture_name: Optional[str] = None) -> bytes:
    """New 'modl' payload with `mesh` (world space, already fitted to the
    skeleton in pose `posed`) replacing the template's geometry.

    Each vertex takes the bone of the nearest template vertex (skin transfer
    from the original model's own skinning); `bone_limits` = {mesh group:
    set of allowed bone names} restricts that for e.g. hair, which should
    ride the head/torso rather than whatever arm it hangs next to.
    Triangles are split across the template's skinned segments by bone
    palette (BMAP), so materials, palettes and chunk layout stay exactly the
    template's. Unskinned segments are kept but emptied to a single
    zero-area triangle. `texture_name` retargets every segment's diffuse
    texture (TNAM slot 0), e.g. at a new atlas."""
    body = parse_body(template_payload)
    segs = [(i, decode_segment(s.payload)) for i, s in enumerate(body.chunks) if s.tag == "segm"]
    # New geometry has no tangents, so bump-mapped segments (vehicles) become
    # plain "Normal" ones with just the diffuse texture - otherwise they'd keep
    # the OLD model's normal map (TNAM slot 1). Same layout as the game's own
    # non-bump segments. `texture_name` also retargets the diffuse (slot 0).
    tnam = struct.pack("<I", 0) + texture_name.encode("ascii") + b"\x00" if texture_name else None
    for _, seg in segs:
        kids = []
        for t, p in seg.children:
            if t == "TNAM":
                if struct.unpack_from("<I", p, 0)[0] != 0:
                    continue
                p = tnam or p
            elif t == "RTYP" and decode_ascii(p) == "Bump":
                p = b"Normal\x00"
            kids.append((t, p))
        seg.children = kids
    skinned = [(i, s) for i, s in segs if s.skinned and s.bmap]
    if not skinned:
        raise SkinnedImportError("Template model has no skinned segments.")
    # importing over an earlier import: its split-off copies of a segment get
    # merged back into one (and re-split below only if still needed), rather
    # than piling up as extra, empty segments
    firsts, duplicates = {}, set()
    for i, s in skinned:
        key = _template_key(s)
        if key in firsts:
            duplicates.add(i)
        else:
            firsts[key] = i
    skinned = [(i, s) for i, s in skinned if i not in duplicates]
    skin_pts = template_skin_points(template_payload, posed)
    vbone = transfer_bones(mesh.positions, skin_pts)
    for group, allowed in (bone_limits or {}).items():
        pts = [sp for sp in skin_pts if bones[sp[1]].name in allowed]
        idx = [i for i, g in enumerate(mesh.groups) if g == group]
        if pts and idx:
            for i, b in zip(idx, transfer_bones([mesh.positions[i] for i in idx], pts)):
                vbone[i] = b
    inv = {}

    def inv_posed(b):
        if b not in inv:
            inv[b] = mat_inverse(posed[b])
        return inv[b]

    palettes = [set(s.bmap) for _, s in skinned]
    opaque = [_is_opaque(s) for _, s in skinned]
    seg_tris = [[] for _ in skinned]
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        tb = [vbone[v] for v in tri]
        # most of the triangle's bones in the palette; ties go to opaque
        # segments (not a vehicle's glass canopy or see-through grate)
        best = max(range(len(skinned)), key=lambda k: (sum(b in palettes[k] for b in tb), opaque[k]))
        seg_tris[best].append(tri)

    built = {}  # template chunk index -> [(segment, verts, indices)] - more than one piece if split
    all_local, world_bind = [], []
    bind = bind_world_matrices(bones)
    for k, (ci, seg) in enumerate(skinned):
        pal = seg.bmap
        pal_pts = [sp for sp in skin_pts if sp[1] in palettes[k]]
        slot = {b: j for j, b in enumerate(pal)}
        pieces = []
        for chunk in _split_triangles(mesh, seg_tris[k]):
            remap, verts, indices = {}, [], []
            for tri in chunk:
                for v in tri:
                    if v not in remap:
                        b = vbone[v]
                        if b not in slot:
                            b = transfer_bones([mesh.positions[v]], pal_pts)[0]
                        m = inv_posed(b)
                        local = mat_point(m, mesh.positions[v])
                        remap[v] = len(verts)
                        verts.append(Vertex(local, mat_dir(m, mesh.normals[v]), mesh.uvs[v], slot[b]))
                        all_local.append(local)
                        world_bind.append(mat_point(bind[b], local))
                    indices.append(remap[v])
            pieces.append((seg, verts, indices))
        built[ci] = pieces or [(seg, [Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), 0)] * 3, [0, 1, 2])]

    for ci, seg in segs:
        if ci in duplicates:
            built[ci] = []  # merged into its first copy above - dropped
        elif ci not in built:
            built[ci] = [(seg, [Vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0), 0)] * 3, [0, 1, 2])]

    lo = tuple(min(p[k] for p in all_local) for k in range(3))
    hi = tuple(max(p[k] for p in all_local) for k in range(3))
    vlo = tuple(min(p[k] for p in world_bind) for k in range(3))
    vhi = tuple(max(p[k] for p in world_bind) for k in range(3))
    face_count = sum(len(ix) // 3 for pieces in built.values() for _, _, ix in pieces)
    segment_count = sum(len(pieces) for pieces in built.values())

    parts = []
    for i, s in enumerate(body.chunks):
        if s.tag == "segm":
            for seg, verts, ix in built[i]:
                parts.append(tagged_block(b"segm", build_segment(seg, verts, ix, (lo, hi))))
            continue
        if s.tag == "INFO":
            info = bytearray(s.payload)
            lead = 12 if len(info) == 68 else 16
            if len(info) == 68:  # (1, segment count, 0) - verified against all 1,677 stock models
                struct.pack_into("<i", info, 4, segment_count)
            struct.pack_into("<6f6f", info, lead, *lo, *hi, *vlo, *vhi)
            # The game picks a unit's detail level from THIS number, not the real
            # geometry: in-game tests showed ~10,000+ here puts a soldier into its
            # far-away mode even up close (weapon hidden, minimal animation - what
            # you see through a sniper scope), while a 52,693-triangle model with
            # the stock count here renders and animates normally. So never report
            # more than the original model did.
            original_faces = struct.unpack_from("<I", s.payload, len(s.payload) - 4)[0]
            struct.pack_into("<I", info, len(info) - 4, min(face_count, original_faces, SAFE_HEADER_FACES))
            payload = bytes(info)
        elif s.tag == "SPHR":
            center = tuple((vlo[k] + vhi[k]) / 2 for k in range(3))
            radius = max(math.sqrt(sum((p[k] - center[k]) ** 2 for k in range(3))) for p in world_bind)
            payload = struct.pack("<4f", *center, radius)
        else:
            payload = s.payload
        parts.append(tagged_block(s.tag.encode("ascii"), payload))
    return body.prefix + b"".join(parts)


def _compressed_diff(a: bytes, b: bytes, stride: int, has_bone: bool) -> list:
    """[max pos diff, bone mismatch (0/1), max normal byte diff, max uv diff]
    between two compressed VBUFs of the same layout."""
    worst = [0, 0, 0, 0]
    for o in range(12, len(a), stride):
        pa, pb = struct.unpack_from("<4h", a, o), struct.unpack_from("<4h", b, o)
        worst[0] = max(worst[0], max(abs(x - y) for x, y in zip(pa, pb)))
        k = o + 8
        if has_bone:
            worst[1] = max(worst[1], int(a[k:k + 4] != b[k:k + 4]))
            k += 4
        worst[2] = max(worst[2], max(abs(x - y) for x, y in zip(a[k:k + 4], b[k:k + 4])))
        ua, ub = struct.unpack_from("<2h", a, k + 4), struct.unpack_from("<2h", b, k + 4)
        worst[3] = max(worst[3], max(abs(x - y) for x, y in zip(ua, ub)))
    return worst


def verify_roundtrip(modl_payload: bytes) -> list:
    """Decode every segment of an original model and re-encode it with the
    encoders above; return a list of (segment, chunk, problem) mismatches.
    Empty list = the encoders reproduce the game's own bytes (compressed
    VBUFs allowed +-1 quantization step)."""
    body = parse_body(modl_payload)
    info = next((s.payload for s in body.chunks if s.tag == "INFO"), None)
    if info is None:
        return []  # empty placeholder model (e.g. fresh from "Add New Chunk") - nothing to check
    lead = 12 if len(info) == 68 else 16
    box = struct.unpack_from("<6f", info, lead)
    vertex_box = (box[:3], box[3:])
    problems = []
    for si, s in enumerate(x for x in body.chunks if x.tag == "segm"):
        seg = decode_segment(s.payload)
        orig = {}
        for t, p in seg.children:
            orig[(t, struct.unpack_from("<I", p, 8)[0]) if t == "VBUF" else (t, None)] = p
        exact = {("VBUF", VBUF_PLAIN): encode_vbuf_plain(seg.vertices)}
        if seg.skinned:
            exact.update({("VBUF", VBUF_SKINNED): encode_vbuf_skinned(seg.vertices),
                          ("VDAT", None): encode_vbuf_plain(seg.vertices),
                          ("SKIN", None): encode_skin(seg.vertices),
                          ("BMAP", None): encode_bmap(seg.bmap)})
        for key, data in exact.items():
            if key in orig and orig[key] != data:
                problems.append((si, key, f"differs ({len(orig[key])} vs {len(data)} bytes)"))
        for flags, stride, has_bone, enc in ((VBUF_COMPRESSED, 20, True, encode_vbuf_compressed),
                                             (VBUF_PLAIN_COMPRESSED, 16, False, encode_vbuf_plain_compressed)):
            a = orig.get(("VBUF", flags))
            if a is None or len(a) < 12 + stride:
                continue
            b = enc(seg.vertices, vertex_box, a[12 + stride - 5])
            if len(a) != len(b):
                problems.append((si, f"VBUF {flags:#x}", "length differs"))
                continue
            worst = _compressed_diff(a, b, stride, has_bone)
            if worst[1] or max(worst[0], worst[2], worst[3]) > 1:
                problems.append((si, f"VBUF {flags:#x}", f"max diff pos/bone/normal/uv = {worst}"))
    return problems


# --- building blocks for the IDE's "Import Character" dialog -----------------------

LOD_SUFFIXES = ("_low1", "_low2", "LOWD", "_lowrez", "_lowres")
TORSO_BONES = {"bone_head", "bone_neck", "bone_ribcage", "bone_b_spine"}


def is_skinned_model(payload: bytes) -> bool:
    return any(s.tag == "segm" and any(ch.tag in ("BMAP", "SKIN") for ch in parse_body(s.payload).chunks)
               for s in parse_body(payload).chunks)


def model_vertex_count(payload: bytes) -> int:
    return sum(len(decode_segment(s.payload).vertices)
               for s in parse_body(payload).chunks if s.tag == "segm")


def model_height(payload: bytes) -> float:
    """Height of the model's bind-pose visibility box (e.g. ~2.1 for a trooper)."""
    info = next(s.payload for s in parse_body(payload).chunks if s.tag == "INFO")
    lead = 12 if len(info) == 68 else 16
    vis = struct.unpack_from("<6f", info, lead + 24)
    return vis[4] - vis[1]


def model_texture(payload: bytes) -> str:
    """Diffuse texture name (TNAM slot 0) of the model's biggest segment -
    its main skin, not e.g. a gunship's small see-through turret balls."""
    best, best_size = "", -1
    for s in parse_body(payload).chunks:
        if s.tag != "segm":
            continue
        for ch in parse_body(s.payload).chunks:
            if ch.tag == "TNAM" and struct.unpack_from("<I", ch.payload, 0)[0] == 0 and len(s.payload) > best_size:
                best, best_size = decode_ascii(ch.payload[4:]), len(s.payload)
    return best


def find_texture(sibling_chunks: list, name: str):
    """The 'tex_' chunk called `name`, ignoring case (models and classes
    don't always spell texture names with the same case as the texture)."""
    low = name.lower()
    return next((ch for ch in sibling_chunks if ch.tag == "tex_" and ch.display_name().lower() == low), None)


def find_lod(sibling_chunks: list, model_name: str):
    """The model's low-detail version next to it (e.g. rep_inf_trooper_low1)."""
    for suffix in LOD_SUFFIXES:
        for ch in sibling_chunks:
            if ch.tag == "modl" and ch.display_name() == model_name + suffix:
                return ch
    return None


def drop_groups(mesh: Mesh, groups) -> Mesh:
    """`mesh` without every triangle that uses one of `groups` (source
    textures), and without the vertices only those triangles used."""
    groups = set(groups)
    if not groups:
        return mesh
    keep = [t for t in range(0, len(mesh.tris), 3) if mesh.groups[mesh.tris[t]] not in groups]
    remap = {}
    for t in keep:
        for v in mesh.tris[t:t + 3]:
            remap.setdefault(v, len(remap))
    order = sorted(remap, key=remap.get)
    pick = lambda values: [values[v] for v in order] if values else []
    return Mesh(pick(mesh.positions), pick(mesh.normals), pick(mesh.uvs),
                [remap[v] for t in keep for v in mesh.tris[t:t + 3]], pick(mesh.groups))


def texture_usage(mesh: Mesh) -> dict:
    """{group: vertex count} - how much of the mesh each source texture covers."""
    out = {}
    for g in mesh.groups:
        out[g] = out.get(g, 0) + 1
    return out


def guess_loose_groups(mesh: Mesh) -> set:
    """Groups that look like hair: nearly all of it above mid-body, reaching
    the top of the head, and hanging a long way down. Those should follow the
    head/torso (TORSO_BONES) instead of whatever limb they hang next to. Only
    a default - the dialog lets you change it. Expects a fitted mesh (feet at 0)."""
    top = max(p[1] for p in mesh.positions) or 1.0
    cx = (min(p[0] for p in mesh.positions) + max(p[0] for p in mesh.positions)) / 2
    by_group = {}
    for p, g in zip(mesh.positions, mesh.groups):
        by_group.setdefault(g, []).append((p[1] / top, abs(p[0] - cx) / top))
    out = set()
    for g, pts in by_group.items():
        ys = sorted(y for y, _ in pts)
        xs = sorted(x for _, x in pts)
        n = len(ys)
        high = sum(1 for y in ys if y > 0.45) / n
        extent = ys[int(n * 0.95)] - ys[int(n * 0.05)]
        # hair hangs near the body's centre line; a texture that also reaches
        # out to the hands (e.g. one material shared by helmet and gloves) isn't hair
        spread = xs[int(n * 0.95)]
        if n >= 20 and high >= 0.95 and extent >= 0.25 and ys[int(n * 0.95)] >= 0.9 and spread <= 0.2:
            out.add(g)
    return out


MODEL_PROPERTIES = {"geometryname", "geometrylowres"}
TEXTURE_PROPERTIES = {"overridetexture"}


def classes_using(sibling_chunks: list, name: str, properties: set) -> list:
    """Class (entc) names in the same container with one of `properties`
    (lower-case .odf property names, e.g. MODEL_PROPERTIES) set to `name`."""
    out = []
    for ch in sibling_chunks:
        if ch.tag != "entc":
            continue
        props = decode_ordnance_payload(ch.payload)["props"]
        if any(k.lower() in properties and v == name for k, v in props):
            out.append(ch.display_name())
    return out


def models_textured_with(sibling_chunks: list, texture: str) -> list:
    """Models in the same container whose diffuse texture (TNAM) is `texture`."""
    return [ch.display_name() for ch in sibling_chunks
            if ch.tag == "modl" and ch.payload and model_texture(ch.payload).lower() == texture.lower()]


def auto_vertex_budget(template_vertices: int, lod: bool = False) -> int:
    """Automatic downscale target. Stock soldiers have ~600-2000 vertices per
    model, and a level only has so much memory for everything in it - a
    model several times bigger has been seen to push other classes out of
    the spawn list. Budgets stay near the model being replaced."""
    return max(900 if lod else 1500, round(template_vertices * (2.2 if lod else 1.4)))


def auto_atlas_layout(usage: dict, images: dict, size: int) -> dict:
    """Packs one region per used texture into a `size`x`size` atlas, area
    roughly proportional to how much of the model uses it (damped so small
    parts still get readable space), each keeping its source aspect ratio.
    Returns {group: (x, y, w, h)}."""
    groups = [g for g in usage if usage[g] > 0]
    weights = {g: usage[g] ** 0.75 for g in groups}
    total = sum(weights.values()) or 1.0
    aspect = {g: (images[g].width / images[g].height) if g in images else 1.0 for g in groups}
    scale = 0.95
    while scale > 0.05:
        rects = {}
        for g in groups:
            area = weights[g] / total * size * size * scale
            w = max(8, min(size, int(math.sqrt(area * aspect[g])) // 4 * 4))
            h = max(8, min(size, int(math.sqrt(area / aspect[g])) // 4 * 4))
            rects[g] = (w, h)
        placed, x, y, shelf = {}, 0, 0, 0
        for g in sorted(groups, key=lambda g: -rects[g][1]):
            w, h = rects[g]
            if x + w > size:
                x, y, shelf = 0, y + shelf, 0
            if y + h > size:
                break
            placed[g] = (x, y, w, h)
            x, shelf = x + w, max(shelf, h)
        if len(placed) == len(groups):
            return placed
        scale *= 0.92
    raise SkinnedImportError("Too many textures to pack into one atlas.")


def fill_missing_images(images: dict, usage: dict) -> dict:
    """Untextured parts (group -1) and textures that aren't embedded in the
    .glb get a plain grey swatch so they still have a place in the atlas."""
    from PIL import Image
    out = dict(images)
    for g in usage:
        if g not in out:
            out[g] = Image.new("RGBA", (8, 8), (170, 170, 170, 255))
    return out


@dataclass
class Zone:
    """A protected area drawn on a view of the source .glb: `rect` is
    (left, bottom, right, top) as fractions of the source's bounding box in
    that view - Front: x across, y up; Side: z across, y up (matching
    render_views). It extends all the way through the model in depth."""
    view: str
    rect: tuple


_ZONE_AXES = {"Front": 0, "Side": 2}


def protected_vertices(mesh: Mesh, zones: list = (), groups=()) -> set:
    """Vertices of every triangle that's inside a Zone (by its centre) or
    uses one of the protected texture `groups` - decimate(locked=...) never
    touches those, so those parts keep their full detail."""
    groups = set(groups)
    if not zones and not groups:
        return set()
    lo = [min(p[k] for p in mesh.positions) for k in range(3)]
    hi = [max(p[k] for p in mesh.positions) for k in range(3)]
    span = [(hi[k] - lo[k]) or 1.0 for k in range(3)]
    boxes = [(_ZONE_AXES[z.view], z.rect) for z in zones]
    out = set()
    for t in range(0, len(mesh.tris), 3):
        tri = mesh.tris[t:t + 3]
        keep = bool(mesh.groups) and mesh.groups[tri[0]] in groups
        if not keep and boxes:
            cx = [sum(mesh.positions[v][k] for v in tri) / 3 for k in range(3)]
            y = (cx[1] - lo[1]) / span[1]
            for axis, (a0, b0, a1, b1) in boxes:
                a = (cx[axis] - lo[axis]) / span[axis]
                if min(a0, a1) <= a <= max(a0, a1) and min(b0, b1) <= y <= max(b0, b1):
                    keep = True
                    break
        if keep:
            out.update(tri)
    return out


def model_index_count(payload: bytes) -> int:
    return sum(len(decode_segment(s.payload).indices) for s in parse_body(payload).chunks if s.tag == "segm")


def model_segment_count(payload: bytes) -> int:
    return sum(1 for s in parse_body(payload).chunks if s.tag == "segm")


# Largest whole models proven in-game so far (the 32,221-vertex / 52,693-
# triangle CE Master Chief on the clone pilot, split into 7 segments, with
# the header triangle count kept at stock - see rebuild_model). Past these
# the budget still works, it's just untested.
TESTED_SOLDIER_VERTICES = 32221
TESTED_VEHICLE_VERTICES = 7969


def tested_vertices(bones: list) -> int:
    return TESTED_SOLDIER_VERTICES if is_character(bones) else TESTED_VEHICLE_VERTICES


def build_character_model(template_payload: bytes, bones: list, mesh: Mesh, target_vertices: int,
                          arm_drop: Optional[float] = None, bone_limits: Optional[dict] = None,
                          texture_name: Optional[str] = None, locked: Optional[set] = None) -> tuple:
    """Decimate (only if bigger than the budget, never touching `locked`
    vertices) + fit the skeleton's arms to the mesh's pose + rebuild. There
    is no cap on detail: segments too big for the game are split (see
    _split_triangles). Returns (payload, stats dict)."""
    src_v, src_t = len(mesh.positions), len(mesh.tris) // 3
    n_locked = len(locked or ())
    # protected vertices come on top of the unprotected part's share - which
    # never drops below half the budget, or a big protected area would leave
    # the rest of the model simplified away to nothing
    target = n_locked + max(target_vertices - n_locked, target_vertices // 2) if n_locked else target_vertices
    dm = decimate(mesh, target, locked=locked) if src_v > target else mesh
    mats = bind_world_matrices(bones)
    drop = estimate_arm_drop(dm, bones, mats) if arm_drop is None else arm_drop
    payload = rebuild_model(template_payload, bones, dm, pose_arms(bones, mats, drop), bone_limits, texture_name)
    return payload, {"source_vertices": src_v, "source_tris": src_t, "vertices": model_vertex_count(payload),
                     "tris": len(dm.tris) // 3, "arm_drop": drop, "bytes": len(payload),
                     "protected": n_locked, "segments": model_segment_count(payload),
                     "indices": model_index_count(payload), "tested": tested_vertices(bones)}


# --- preview renderer ------------------------------------------------------------------

_PREVIEW_POSE = {"bone_r_upperarm": ("z", -55), "bone_l_forearm": ("y", 60),
                 "bone_r_thigh": ("x", -35), "bone_r_calf": ("x", 55), "bone_head": ("y", 35)}


def _axis_rot(axis: str, deg: float) -> list:
    a = math.radians(deg)
    ca, sa = math.cos(a), math.sin(a)
    r = {"x": [[1, 0, 0], [0, ca, -sa], [0, sa, ca]],
         "y": [[ca, 0, sa], [0, 1, 0], [-sa, 0, ca]],
         "z": [[ca, -sa, 0], [sa, ca, 0], [0, 0, 1]]}[axis]
    return [r[0] + [0.0], r[1] + [0.0], r[2] + [0.0], [0.0, 0.0, 0.0, 1.0]]


def model_triangles(payload: bytes, bones: list, pose: Optional[dict] = None) -> list:
    """[(a, b, c, uv centroid)] world-space triangles decoded from a modl's
    own bytes, optionally with bones rotated ({bone name: (axis, degrees)})."""
    names = [b.name for b in bones]
    overrides = {names.index(n): _axis_rot(ax, d) for n, (ax, d) in (pose or {}).items() if n in names}
    mats = bind_world_matrices(bones, overrides)
    body = parse_body(payload)
    node = decode_ascii(next(s.payload for s in body.chunks if s.tag == "NODE"))
    out = []
    for s in body.chunks:
        if s.tag != "segm":
            continue
        seg = decode_segment(s.payload)
        if seg.skinned:
            world = [mat_point(mats[seg.bmap[v.bone]], v.pos) for v in seg.vertices]
        else:
            bnam = next((p for t, p in seg.children if t == "BNAM"), None)
            bname = decode_ascii(bnam) if bnam else node
            m = mats[names.index(bname)] if bname in names else _IDENTITY
            world = [mat_point(m, v.pos) for v in seg.vertices]
        tris = triangles(seg)
        for i in range(0, len(tris) - 2, 3):
            ids = tris[i:i + 3]
            uv = (sum(seg.vertices[j].uv[0] for j in ids) / 3, sum(seg.vertices[j].uv[1] for j in ids) / 3)
            out.append((world[ids[0]], world[ids[1]], world[ids[2]], uv))
    return out


_VIEWS = {  # (screen x, screen y, depth towards the viewer) from world (x, y, z)
    "Front": lambda p: (p[0], p[1], p[2]),
    "Side": lambda p: (p[2], p[1], -p[0]),
    "Back": lambda p: (-p[0], p[1], -p[2]),
    "Top": lambda p: (p[0], p[2], p[1]),
}


def mesh_triangles(mesh: Mesh) -> list:
    """[(a, b, c, uv centroid)] for a Mesh, in the same shape model_triangles gives."""
    out = []
    P, U = mesh.positions, mesh.uvs
    for t in range(0, len(mesh.tris), 3):
        i, j, k = mesh.tris[t:t + 3]
        out.append((P[i], P[j], P[k], ((U[i][0] + U[j][0] + U[k][0]) / 3, (U[i][1] + U[j][1] + U[k][1]) / 3)))
    return out


def render_preview(payload: bytes, bones: list, texture=None, panel: tuple = (230, 300), markers=()):
    """Flat-shaded, textured front / side / back views in bind pose, plus a
    4th view: for characters a front view with a few bones rotated (shows the
    skinning follows the bones), otherwise a top view. `markers` =
    [(world point, label, (r, g, b))] drawn on the bind-pose views (e.g.
    weapon fire points). `texture` is a PIL image (the atlas) or None."""
    names = {b.name for b in bones}
    posable = all(n in names for n in _PREVIEW_POSE)
    bind = model_triangles(payload, bones)
    views = [("Front", "Front", bind, True), ("Side", "Side", bind, True), ("Back", "Back", bind, True)]
    if posable:
        views.append(("Test pose", "Front", model_triangles(payload, bones, _PREVIEW_POSE), False))
    else:
        views.append(("Top", "Top", bind, True))
    return render_views(views, texture, panel, markers)[0]


def render_views(views: list, texture=None, panel: tuple = (230, 300), markers=(), draw_markers: bool = True) -> tuple:
    """Draws `views` = [(label, view name (a _VIEWS key), triangles, show
    markers?)] side by side, each scaled to fit its panel. Returns (image,
    frames) where frames[i] = (view name, scale, ox, oy): a world point p
    lands at pixel (ox + a * scale, oy - b * scale), (a, b, _) = _VIEWS[view](p)."""
    from PIL import Image, ImageDraw
    W, H = panel
    img = Image.new("RGB", (W * len(views), H + 18), (28, 28, 34))
    d = ImageDraw.Draw(img)
    tex = texture.convert("RGB") if texture is not None else None
    light = (0.4, 0.5, 0.75)
    frames = []
    for vi, (label, view, tris, show_markers) in enumerate(views):
        project = _VIEWS[view]
        proj = []
        scale, ox, oy = 1.0, W * vi + W / 2, 18 + H / 2
        for a, b, cc, uv in tris:
            P = [project(p) for p in (a, b, cc)]
            ux, uy, uz = (P[1][k] - P[0][k] for k in range(3))
            vx, vy, vz = (P[2][k] - P[0][k] for k in range(3))
            nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
            ln = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
            shade = 0.35 + 0.65 * abs(nx * light[0] + ny * light[1] + nz * light[2]) / ln
            proj.append((sum(p[2] for p in P), P, shade, uv))
        if proj:
            xs = [p[0] for _, P, _, _ in proj for p in P]
            ys = [p[1] for _, P, _, _ in proj for p in P]
            if show_markers:  # keep far-off fire points in frame too
                xs += [project(m[0])[0] for m in markers]
                ys += [project(m[0])[1] for m in markers]
            w, h = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
            scale = min((W - 12) / w, (H - 12) / h)
            ox = W * vi + W / 2 - (min(xs) + max(xs)) / 2 * scale
            oy = 18 + H / 2 + (min(ys) + max(ys)) / 2 * scale
        proj.sort(key=lambda t: t[0])
        for _, P, shade, uv in proj:
            if tex is not None:
                r, g, b = tex.getpixel((min(tex.width - 1, max(0, int(uv[0] * tex.width))),
                                        min(tex.height - 1, max(0, int(uv[1] * tex.height)))))
            else:
                r = g = b = 225
            d.polygon([(ox + p[0] * scale, oy - p[1] * scale) for p in P],
                      fill=(int(r * shade), int(g * shade), int(b * shade)))
        if show_markers and draw_markers:
            for point, text, color in markers:
                a, b, _ = project(point)
                x, y = ox + a * scale, oy - b * scale
                d.ellipse((x - 4, y - 4, x + 4, y + 4), outline=color, width=2)
                d.line((x - 7, y, x + 7, y), fill=color)
                d.line((x, y - 7, x, y + 7), fill=color)
                if text:
                    d.text((x + 6, y - 12), text, fill=color)
        d.text((W * vi + 6, 3), label, fill=(220, 220, 220))
        frames.append((view, scale, ox, oy))
    return img, frames


# --- collision -------------------------------------------------------------------------
# 'coll' (verified by decoding every stock one back exactly, see
# swbf-unmunge's handle_collision.cpp for the reader): NAME, [MASK], NODE,
# INFO (u32 vertex count, node count, leaf count, index count, then the
# float min/max box), POSI (vertex_count float3), TREE. TREE is a binary
# bounding-volume tree stored parent-first: a NODE is 6 bytes (u8 min
# xyz, u8 max xyz), a LEAF is u8 point count + those 6 box bytes + u16
# polygon indices; every box is quantized to 0..255 of its PARENT's box
# (the root's parent is INFO's box). Leaf polygons wind the opposite way
# to glTF (the game is clockwise-front).
# 'prim' (handle_primitives.cpp): INFO (owner model \0, i32 count), then per
# shape NAME, [MASK], PRNT (bone), XFRM (12 floats, bone-local), DATA (u32
# type: 0 sphere / 2 cylinder / 4 box, then 3 floats of size). Classes
# refer to shapes by name (vehiclecollision = p_vehiclesphere, ...).

def find_collision(sibling_chunks: list, model_name: str) -> tuple:
    """(the model's 'coll' chunk or None, its 'prim' chunk or None)."""
    coll = next((ch for ch in sibling_chunks if ch.tag == "coll" and ch.display_name() == model_name), None)
    prim = None
    for ch in sibling_chunks:
        if ch.tag == "prim":
            info = next((s.payload for s in parse_body(ch.payload).chunks if s.tag == "INFO"), b"")
            if info.split(b"\x00", 1)[0].decode("ascii", "replace") == model_name:
                prim = ch
                break
    return coll, prim


def model_has_shadow(payload: bytes) -> bool:
    return any(s.tag == "shdw" for s in parse_body(payload).chunks)


def strip_shadows(payload: bytes) -> bytes:
    """The model without its 'shdw' shadow-volume pieces - those still have
    the OLD model's shape. Plenty of stock models ship without any (e.g. the
    low-detail soldiers)."""
    body = parse_body(payload)
    return body.prefix + b"".join(tagged_block(s.tag.encode("ascii"), s.payload)
                                  for s in body.chunks if s.tag != "shdw")


def _weld(tris: list, tolerance: float) -> Mesh:
    """World-space triangle soup -> welded Mesh (shared vertices), so the
    collision mesh is one connected surface the simplifier can work on."""
    index, positions, flat = {}, [], []
    for tri in tris:
        for p in tri[:3]:
            key = tuple(round(v / tolerance) for v in p)
            if key not in index:
                index[key] = len(positions)
                positions.append(tuple(p))
            flat.append(index[key])
    out = []
    for t in range(0, len(flat), 3):
        if len({flat[t], flat[t + 1], flat[t + 2]}) == 3:
            out += flat[t:t + 3]
    return Mesh(positions, [(0.0, 1.0, 0.0)] * len(positions), [(0.0, 0.0)] * len(positions), out,
                [0] * len(positions))


def _quantize_box(box: tuple, parent: tuple) -> tuple:
    """(6 quantized bytes, the dequantized box they stand for) - rounded
    outward so the stored box always contains the real one."""
    (lo, hi), (plo, phi) = box, parent
    q_lo, q_hi = [], []
    for k in range(3):
        span = phi[k] - plo[k]
        if span <= 0:
            q_lo.append(0)
            q_hi.append(255)
            continue
        q_lo.append(max(0, min(255, math.floor((lo[k] - plo[k]) / span * 255.0))))
        q_hi.append(max(0, min(255, math.ceil((hi[k] - plo[k]) / span * 255.0))))
    deq = (tuple(plo[k] + q_lo[k] / 255.0 * (phi[k] - plo[k]) for k in range(3)),
           tuple(plo[k] + q_hi[k] / 255.0 * (phi[k] - plo[k]) for k in range(3)))
    return bytes(q_lo + q_hi), deq


def build_collision_tree(positions: list, tris: list) -> tuple:
    """(TREE payload, node count, leaf count, index count, root box) for a
    triangle list: one triangle per leaf, split at the median along the
    longest axis."""
    faces = [tuple(tris[i:i + 3]) for i in range(0, len(tris), 3)]
    boxes = []
    for f in faces:
        pts = [positions[v] for v in f]
        boxes.append((tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3))))
    cents = [tuple((b[0][k] + b[1][k]) / 2 for k in range(3)) for b in boxes]
    root = (tuple(min(p[k] for p in positions) for k in range(3)), tuple(max(p[k] for p in positions) for k in range(3)))
    out, counts = [], [0, 0]

    def union(ids):
        return (tuple(min(boxes[i][0][k] for i in ids) for k in range(3)),
                tuple(max(boxes[i][1][k] for i in ids) for k in range(3)))

    def build(ids, parent):
        q, deq = _quantize_box(union(ids), parent)
        if len(ids) == 1:
            a, b, cc = faces[ids[0]]
            out.append(tagged_block(b"LEAF", bytes([3]) + q + struct.pack("<3H", a, cc, b)))
            counts[1] += 1
            return
        out.append(tagged_block(b"NODE", q))
        counts[0] += 1
        axis = max(range(3), key=lambda k: max(cents[i][k] for i in ids) - min(cents[i][k] for i in ids))
        ids = sorted(ids, key=lambda i: cents[i][axis])
        mid = len(ids) // 2
        build(ids[:mid], deq)
        build(ids[mid:], deq)

    build(list(range(len(faces))), root)
    return b"".join(out), counts[0], counts[1], 3 * counts[1], root


def rebuild_collision_mesh(template_payload: bytes, bones: list, world_tris: list, target_vertices: int) -> tuple:
    """New 'coll' payload from the new model's bind-pose triangles: welded,
    simplified to `target_vertices`, placed relative to the chunk's NODE bone,
    written in the template's layout. Returns (payload, stats)."""
    body = parse_body(template_payload)
    kids = [(s.tag, s.payload) for s in body.chunks]
    node = decode_ascii(next(p for t, p in kids if t == "NODE"))
    names = [b.name for b in bones]
    to_local = mat_inverse(bind_world_matrices(bones)[names.index(node)]) if node in names else _IDENTITY
    size = max(max(p[k] for t in world_tris for p in t[:3]) - min(p[k] for t in world_tris for p in t[:3])
               for k in range(3)) or 1.0
    mesh = _weld(world_tris, size * 1e-4)
    mesh = decimate(mesh, target_vertices, boundary_weight=10.0) if len(mesh.positions) > target_vertices else mesh
    if len(mesh.tris) < 3:
        raise SkinnedImportError("The new model left nothing to build a collision mesh from.")
    local = [mat_point(to_local, p) for p in mesh.positions]
    tree, node_count, leaf_count, index_count, root = build_collision_tree(local, mesh.tris)
    info = struct.pack("<4I6f", len(local), node_count, leaf_count, index_count, *root[0], *root[1])
    posi = b"".join(struct.pack("<3f", *p) for p in local)
    parts = []
    for t, p in kids:
        if t == "INFO":
            p = info
        elif t == "POSI":
            p = posi
        elif t == "TREE":
            p = tree
        parts.append(tagged_block(t.encode("ascii"), p))
    return body.prefix + b"".join(parts), {"vertices": len(local), "triangles": leaf_count}


def decode_collision_mesh(payload: bytes, strict: bool = False) -> tuple:
    """(positions, triangles) of a 'coll' chunk. `strict` also checks every
    box on the way down contains its polygons - true of everything this
    module writes and of 68 of the 71 stock meshes; the other 3 (e.g.
    all_fly_ywing) have boxes the original tools got wrong, which the game
    evidently tolerates."""
    kids = {s.tag: s.payload for s in parse_body(payload).chunks}
    vc, nc, lc, ic = struct.unpack_from("<4I", kids["INFO"])
    box = struct.unpack_from("<6f", kids["INFO"], 16)
    pos = [struct.unpack_from("<3f", kids["POSI"], i * 12) for i in range(vc)]
    items = [(s.tag, s.payload) for s in parse_body(kids["TREE"]).chunks]
    tris, i = [], [0]

    def dq(q, parent):
        lo, hi = parent
        return (tuple(lo[k] + q[k] / 255.0 * (hi[k] - lo[k]) for k in range(3)),
                tuple(lo[k] + q[3 + k] / 255.0 * (hi[k] - lo[k]) for k in range(3)))

    def walk(parent):
        tag, p = items[i[0]]
        i[0] += 1
        if tag == "NODE":
            b = dq(p[:6], parent)
            walk(b)
            walk(b)
            return
        b = dq(p[1:7], parent)
        ids = struct.unpack_from(f"<{p[0]}H", p, 7)
        for v in ids if strict else ():
            for k in range(3):
                span = (parent[1][k] - parent[0][k]) or 1.0
                if not (b[0][k] - span / 254 <= pos[v][k] <= b[1][k] + span / 254):
                    raise SkinnedImportError("Collision tree box doesn't contain its polygon.")
        for j in range(2, len(ids)):
            tris.append((ids[0], ids[j], ids[j - 1]))

    walk((box[:3], box[3:]))
    nodes = sum(1 for t, _ in items if t == "NODE")
    leaves = len(items) - nodes
    points = sum(p[0] for t, p in items if t == "LEAF")
    if i[0] != len(items) or (nc, lc, ic) != (nodes, leaves, points):
        raise SkinnedImportError("Collision tree counts don't match its INFO.")
    return pos, tris


def refit_primitives(prim_payload: bytes, bones: list, old_bounds: tuple, new_bounds: tuple) -> tuple:
    """Moves and stretches each collision shape (sphere / cylinder / box) so
    it sits in the same relative spot of the new model's box as it did in
    the old one's - same names, bones and types, so every class reference
    still works. Returns (payload, number of shapes refitted)."""
    (olo, ohi), (nlo, nhi) = old_bounds, new_bounds
    ratio = [((nhi[k] - nlo[k]) / (ohi[k] - olo[k])) if ohi[k] - olo[k] > 1e-6 else 1.0 for k in range(3)]
    uniform = (ratio[0] * ratio[1] * ratio[2]) ** (1.0 / 3.0)
    oc = [(olo[k] + ohi[k]) / 2 for k in range(3)]
    nc = [(nlo[k] + nhi[k]) / 2 for k in range(3)]
    names = [b.name for b in bones]
    mats = bind_world_matrices(bones)
    body = parse_body(prim_payload)
    kids = [(s.tag, s.payload) for s in body.chunks]
    parts, bone, xfrm_world, count = [], None, None, 0
    for t, p in kids:
        if t == "PRNT":
            bone = decode_ascii(p)
        elif t == "XFRM" and bone in names:
            parent = mats[names.index(bone)]
            local = _mat_from_transform(struct.unpack_from("<12f", p))
            xfrm_world = mat_mul(parent, local)
            center = (xfrm_world[0][3], xfrm_world[1][3], xfrm_world[2][3])
            new_center = tuple(nc[k] + (center[k] - oc[k]) * ratio[k] for k in range(3))
            nl = mat_point(mat_inverse(parent), new_center)
            vals = list(struct.unpack_from("<12f", p))
            vals[9:12] = nl
            p = struct.pack("<12f", *vals)
        elif t == "DATA" and xfrm_world is not None:
            kind, sx, sy, sz = struct.unpack_from("<I3f", p)
            if kind == 4:  # box: stretch each of its own axes by how much the model stretched along it
                axes = [[xfrm_world[r][k] for r in range(3)] for k in range(3)]
                f = []
                for ax in axes:
                    n = math.sqrt(sum(v * v for v in ax)) or 1.0
                    f.append(math.sqrt(sum((ax[j] / n * ratio[j]) ** 2 for j in range(3))))
                sx, sy, sz = sx * f[0], sy * f[1], sz * f[2]
            else:  # sphere / cylinder
                sx, sy, sz = sx * uniform, sy * uniform, sz * uniform
            p = struct.pack("<I3f", kind, sx, sy, sz) + p[16:]
            xfrm_world = None
            count += 1
        parts.append(tagged_block(t.encode("ascii"), p))
    return body.prefix + b"".join(parts), count


def model_bounds(payload: bytes, bones: list) -> tuple:
    tris = model_triangles(payload, bones)
    pts = [p for t in tris for p in t[:3]]
    return (tuple(min(p[k] for p in pts) for k in range(3)), tuple(max(p[k] for p in pts) for k in range(3)))


def auto_collision_budget(template_coll_payload: bytes) -> int:
    kids = {s.tag: s.payload for s in parse_body(template_coll_payload).chunks}
    vc = struct.unpack_from("<I", kids["INFO"])[0] if "INFO" in kids else 60
    return max(40, min(600, vc * 2))


# --- live orientation view -----------------------------------------------------------

def orientation_triangles(mesh: Mesh, images: dict, limit: int = 3000) -> tuple:
    """Lightweight stand-in for the live 3D orientation view: (up to 5000
    evenly sampled positions - enough to fit it like the real import does -,
    the `limit` biggest triangles, each coloured with its texture's average
    colour)."""
    colors = {}
    for g, im in images.items():
        r, gg, b, *_ = im.convert("RGBA").resize((1, 1)).getpixel((0, 0))
        colors[g] = (r, gg, b)
    P = mesh.positions
    tris = []
    for t in range(0, len(mesh.tris), 3):
        a, b, cc = (P[v] for v in mesh.tris[t:t + 3])
        n = _face_normal(a, b, cc)
        area = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2)
        g = mesh.groups[mesh.tris[t]] if mesh.groups else -1
        tris.append((area, a, b, cc, colors.get(g, (170, 170, 170))))
    tris.sort(key=lambda x: -x[0])
    step = max(1, len(P) // 5000)
    return P[::step], [t[1:] for t in tris[:limit]]


def render_orientation(fit_points: list, tris: list, turns: tuple, lo: tuple, hi: tuple, axis: str,
                       size: Optional[float], yaw: float, pitch: float, panel: tuple):
    """One frame of the live orientation view: the model turned by `turns`
    (x, y, z degrees) and fitted into the original's box exactly like
    fit_to_bounds, seen from a camera at `yaw`/`pitch`. Shows the ground, the
    original model's box, a yellow arrow for the way the game treats as
    forward, and a red/green/blue X/Y/Z axis marker. Returns a PIL image."""
    from PIL import Image, ImageDraw
    W, H = panel
    r = rotation_matrix(*turns)

    def rot(v):
        return (r[0][0] * v[0] + r[0][1] * v[1] + r[0][2] * v[2],
                r[1][0] * v[0] + r[1][1] * v[1] + r[1][2] * v[2],
                r[2][0] * v[0] + r[2][1] * v[1] + r[2][2] * v[2])

    pts = [rot(p) for p in fit_points]
    mlo = [min(p[k] for p in pts) for k in range(3)]
    mhi = [max(p[k] for p in pts) for k in range(3)]
    ext = [mhi[k] - mlo[k] for k in range(3)]
    k_axis = FIT_AXES[axis]
    target_k = k_axis
    if k_axis is None:
        k_axis = max(range(3), key=lambda k: ext[k])
        target_k = max(range(3), key=lambda k: hi[k] - lo[k])
    s = (size if size else hi[target_k] - lo[target_k]) / (ext[k_axis] or 1.0)
    cx, cz = (mlo[0] + mhi[0]) / 2, (mlo[2] + mhi[2]) / 2
    tx, tz = (lo[0] + hi[0]) / 2, (lo[2] + hi[2]) / 2

    def place(p):
        x, y, z = rot(p)
        return ((x - cx) * s + tx, (y - mlo[1]) * s + lo[1], (z - cz) * s + tz)

    center = [(lo[k] + hi[k]) / 2 for k in range(3)]
    cy, sy_ = math.cos(math.radians(yaw)), math.sin(math.radians(yaw))
    cp, sp = math.cos(math.radians(pitch)), math.sin(math.radians(pitch))

    def view(p):  # camera: yaw about Y, then pitch about X; +z out of the screen
        x, y, z = (p[k] - center[k] for k in range(3))
        x, z = x * cy - z * sy_, x * sy_ + z * cy
        y, z = y * cp - z * sp, y * sp + z * cp
        return x, y, z

    radius = max(math.dist(lo, hi) / 2, max(ext) * s * 0.75) or 1.0
    scale = min(W, H) * 0.42 / radius

    def screen(v):
        return (W / 2 + v[0] * scale, H * 0.52 - v[1] * scale)

    img = Image.new("RGB", (W, H), (28, 28, 34))
    d = ImageDraw.Draw(img)
    # ground grid
    g = max(hi[0] - lo[0], hi[2] - lo[2]) * 0.75 or 1.0
    for i in range(-4, 5):
        f = i / 4 * g
        for a, b in (((center[0] + f, lo[1], center[2] - g), (center[0] + f, lo[1], center[2] + g)),
                     ((center[0] - g, lo[1], center[2] + f), (center[0] + g, lo[1], center[2] + f))):
            d.line([screen(view(a)), screen(view(b))], fill=(55, 55, 64))
    # the original model's box
    corners = [(x, y, z) for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    for i, a in enumerate(corners):
        for j, b in enumerate(corners):
            if i < j and sum(a[k] != b[k] for k in range(3)) == 1:
                d.line([screen(view(a)), screen(view(b))], fill=(90, 90, 110))
    # the model
    light = (0.35, 0.6, 0.72)
    drawn = []
    for a, b, cc, col in tris:
        va, vb, vc = view(place(a)), view(place(b)), view(place(cc))
        n = _face_normal(va, vb, vc)
        ln = math.sqrt(n[0] ** 2 + n[1] ** 2 + n[2] ** 2) or 1.0
        shade = 0.35 + 0.65 * abs(n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / ln
        drawn.append((va[2] + vb[2] + vc[2], (screen(va), screen(vb), screen(vc)),
                      tuple(int(ch * shade) for ch in col)))
    drawn.sort(key=lambda t: t[0])
    for _, poly, col in drawn:
        d.polygon(poly, fill=col)
    # forward arrow along +z at ground level
    length = max(hi[2] - lo[2], hi[0] - lo[0]) * 0.75
    base = (center[0], lo[1], center[2])
    tip = (center[0], lo[1], center[2] + length)
    wing = length * 0.18
    b2, t2 = screen(view(base)), screen(view(tip))
    d.line([b2, t2], fill=(255, 210, 60), width=3)
    for side in (-1, 1):
        d.line([t2, screen(view((center[0] + side * wing, lo[1], center[2] + length - wing)))],
               fill=(255, 210, 60), width=3)
    d.text((t2[0] + 4, t2[1] - 12), "Front", fill=(255, 210, 60))
    # axis marker
    ox, oy, ln = 26, H - 26, 18
    for vec, col, label in (((1, 0, 0), (235, 80, 80), "X"), ((0, 1, 0), (90, 210, 100), "Y"),
                            ((0, 0, 1), (90, 140, 255), "Z")):
        v = view(tuple(center[k] + vec[k] for k in range(3)))
        ex, ey = ox + v[0] * ln, oy - v[1] * ln
        d.line([(ox, oy), (ex, ey)], fill=col, width=2)
        d.text((ex + 2, ey - 6), label, fill=col)
    return img


# --- weapon / fire points --------------------------------------------------------------

_FIRE_WORDS = ("fire", "gun", "cannon", "missile", "weapon", "barrel", "turret", "launcher")


def weapon_points(bones: list, sibling_chunks: list, model_name: str) -> list:
    """[(bone name, bind-pose world position)] for the points a model's
    weapons fire from / attach to: every bone the classes using this model
    name as FirePointName, plus 'hp_*' bones that look weapon-related
    (hp_cannon_1, hp_fire, hp_weapons...). These stay exactly where the
    original model had them - the new geometry doesn't move them."""
    wanted = set()
    for ch in sibling_chunks:
        if ch.tag != "entc":
            continue
        props = decode_ordnance_payload(ch.payload)["props"]
        if not any(k.lower() in MODEL_PROPERTIES and v == model_name for k, v in props):
            continue
        wanted.update(v for k, v in props if k.lower() == "firepointname")
    mats = bind_world_matrices(bones)
    out = []
    for i, b in enumerate(bones):
        low = b.name.lower()
        if b.name in wanted or (low.startswith("hp_") and any(w in low for w in _FIRE_WORDS)):
            out.append((b.name, (mats[i][0][3], mats[i][1][3], mats[i][2][3])))
    return out


def hard_points(bones: list, sibling_chunks: list, model_name: str) -> list:
    """[(bone name, bind-pose world position, is a fire point?)] for every
    'hp_*' bone (fire points, gunner/passenger seats, damage smoke...) plus
    any other bone a class names as its FirePointName. None of these are
    ever skinned to (checked on all 1,039 in the stock game), so moving one
    moves only that point, never any geometry."""
    fire = {name for name, _ in weapon_points(bones, sibling_chunks, model_name)}
    mats = bind_world_matrices(bones)
    return [(b.name, (mats[i][0][3], mats[i][1][3], mats[i][2][3]), b.name in fire)
            for i, b in enumerate(bones) if b.name in fire or b.name.lower().startswith("hp_")]


def find_skeleton_chunk(sibling_chunks: list, model_name: str):
    """The 'skel' chunk whose INFO names `model_name`, or None."""
    for ch in sibling_chunks:
        if ch.tag != "skel":
            continue
        try:
            owner, _ = decode_skeleton_chunk(ch.payload)
        except (SkinnedImportError, ValueError, struct.error):
            continue
        if owner == model_name:
            return ch
    return None


def move_bones(bones: list, targets: dict) -> list:
    """A copy of `bones` with each bone named in `targets` (name -> bind-pose
    world position) moved there. Only the translation changes (the point
    keeps its aim direction), and any child bones are compensated so they
    stay exactly where they were."""
    mats = bind_world_matrices(bones)
    index = {b.name: i for i, b in enumerate(bones)}
    # rotations never change, so every bone's world matrix is its old one
    # with (for the moved ones) a new translation
    world = [[row[:] for row in m] for m in mats]
    for name, pos in targets.items():
        if name in index:
            for k in range(3):
                world[index[name]][k][3] = pos[k]
    out = list(bones)
    for i, b in enumerate(bones):
        p = index.get(b.parent)
        if b.name not in targets and (p is None or bones[p].name not in targets):
            continue  # neither it nor its parent moved
        pos = tuple(world[i][k][3] for k in range(3))
        t = list(b.transform)
        t[9:12] = mat_point(mat_inverse(world[p]), pos) if p is not None else pos
        out[i] = Bone(b.name, b.parent, tuple(t))
    return out


def encode_skeleton_transforms(skel_payload: bytes, bones: list) -> bytes:
    """`skel_payload` with its XFRM replaced by `bones`' transforms (same
    bones, same order - only positions/rotations change)."""
    body = parse_body(skel_payload)
    xfrm = b"".join(struct.pack("<12f", *b.transform) for b in bones)
    parts = []
    for s in body.chunks:
        if s.tag == "XFRM":
            if len(s.payload) != len(xfrm):
                raise SkinnedImportError("Skeleton bone count changed - can't rewrite its transforms.")
            parts.append(tagged_block(b"XFRM", xfrm))
        else:
            parts.append(tagged_block(s.tag.encode("ascii"), s.payload))
    return body.prefix + b"".join(parts)


def nearest_surface_point(point: tuple, payload: bytes, bones: list) -> tuple:
    """The model vertex (bind pose, world space) closest to `point`."""
    verts = [p for t in model_triangles(payload, bones) for p in t[:3]]
    if not verts:
        return tuple(point)
    return tuple(min(verts, key=lambda q: (q[0] - point[0]) ** 2 + (q[1] - point[1]) ** 2 + (q[2] - point[2]) ** 2))


def weapon_point_report(points: list, payload: bytes, bones: list) -> list:
    """[(bone name, distance to the new model's surface vertices, off_model?)]
    - a fire point well away from the new model means shots will appear to
    come out of thin air."""
    tris = model_triangles(payload, bones)
    verts = [p for t in tris for p in t[:3]]
    if not verts:
        return []
    size = max(max(p[k] for p in verts) - min(p[k] for p in verts) for k in range(3))
    limit = max(0.15, size * 0.05)
    try:
        import numpy as np
        V = np.asarray(verts)
        dist = lambda p: float(np.sqrt(((V - np.asarray(p)) ** 2).sum(axis=1)).min())
    except ImportError:
        dist = lambda p: min(math.dist(p, q) for q in verts)
    return [(name, d, d > limit) for name, p in points for d in [dist(p)]]


# ============================================================================================
# The Debug Menu mod - built from the player's own files, so no game files are shipped.
#
# The pause menu still has the developers' Fake Console and Free Camera buttons, hidden on
# the final PC build. This shows them, gives the console button a real label, puts a list of
# commands that are safe in a running battle at the top of the console, and runs the chosen
# command after the menu has closed and the game is unpaused (run inside the paused menu,
# several do nothing or hang it). The console's commands only work with the console fix
# (Loader.patch_console / the "Fake console fix" cheat) applied to the running game.
# ============================================================================================

DEBUG_MENU_MOD = "Debug Menu"
DEBUG_MENU_LABEL_KEY = "game.pause.console"
DEBUG_MENU_SAFE_COMMANDS = ("invincible", "unlimitedammo", "framerate", "statsline", "showpolycount",
                            "rendercollision", "showflyerheights", "freecamlight", "ai.showhints",
                            "ai.showallpaths", "ai.showobstacles", "ai.notext")


def _replace_once(src: str, old: str, new: str, what: str, count: int = 1) -> str:
    if src.count(old) != count:
        raise ValueError(f"{what}: this game's script doesn't look like the 1.3 one - not changing it")
    return src.replace(old, new)


def _debug_pausemenu(src: str) -> str:
    src = _replace_once(src, '{tag = "console", string = "Fake Console"}',
                        f'{{tag = "console", string = "{DEBUG_MENU_LABEL_KEY}"}}', "pause menu buttons", 2)
    src = _replace_once(src, "p1.buttons.freecam.hidden = gDemoBuild or gFinalBuild",
                        "p1.buttons.freecam.hidden = nil", "free camera button")
    return _replace_once(src, "p1.buttons.console.hidden = gDemoBuild or gFinalBuild",
                         "p1.buttons.console.hidden = nil", "console button")


def _debug_fakeconsole(src: str) -> str:
    src = _replace_once(src, "IFText_fnSetString(p1.showstr, p2.ShowStr)",
                        "IFText_fnSetUString(p1.showstr, ScriptCB_tounicode(p2.ShowStr))", "console list")
    src = _replace_once(src, "    local v1 = gConsoleCmdList[fakeconsole_listbox_layout.SelectedIdx]\n"
                             '    ScriptCB_SndPlaySound("shell_menu_enter")\n'
                             "    ScriptCB_DoConsoleCmd(v1.ShowStr)\n"
                             "    ScriptCB_PopScreen()\n",
                        "    local v1 = gConsoleCmdList[fakeconsole_listbox_layout.SelectedIdx]\n"
                        "    if not v1 then\n"
                        "      ScriptCB_PopScreen()\n"
                        "      return\n"
                        "    end\n"
                        "    local v2 = v1.ShowStr\n"
                        '    ScriptCB_SndPlaySound("shell_menu_enter")\n'
                        "    ScriptCB_PopScreen()\n"
                        "    ScriptCB_Unpause()\n"
                        "    ScriptCB_DoConsoleCmd(v2)\n", "console command")
    safe = "\n".join(f'  "{c}",' for c in DEBUG_MENU_SAFE_COMMANDS)
    return src.rstrip("\n") + f"""
gConsoleSafeCmds = {{
{safe}
}}
gConsoleAllCmds = {{}}
for i1 = 1, getn(gConsoleSafeCmds) do
  gConsoleAllCmds[i1] = {{ShowStr = gConsoleSafeCmds[i1]}}
end
for i2 = 1, getn(gConsoleCmdList) do
  gConsoleAllCmds[getn(gConsoleSafeCmds) + i2] = gConsoleCmdList[i2]
end
gConsoleCmdList = gConsoleAllCmds
"""


def _lvl_pc_dir(folder) -> Path:
    """The _LVL_PC folder inside an originals folder (or a game folder, or _LVL_PC itself)."""
    folder = Path(folder)
    for d in (folder, folder / "_LVL_PC", folder / "Data" / "_LVL_PC", folder / "GameData" / "Data" / "_LVL_PC"):
        if (d / "core.lvl").is_file() and (d / "Common").is_dir():
            return d
    raise ValueError(f"No game files (Data/_LVL_PC/core.lvl) in {folder}")


def make_debug_menu(originals, out_dir) -> list:
    """Writes the Debug Menu mod into out_dir from untouched game files in `originals`:
    each common.lvl that holds the pause menu, with it and the console's script changed,
    and core.lvl with the console button's label. Returns the files written, relative to out_dir."""
    src_dir, out_dir = _lvl_pc_dir(originals), Path(out_dir)
    edits = {"ifs_pausemenu": _debug_pausemenu, "ifs_fakeconsole": _debug_fakeconsole}
    written = []
    commons = sorted(src_dir.glob("Common/*/common.lvl"))
    if not commons:
        raise ValueError(f"No Common/<language>/common.lvl in {src_dir}")
    for path in commons:   # the menus are in some languages' common.lvl, not all (FRE has none)
        container = parse_container(path.read_bytes())
        done = set()
        for _p, _i, ch, _n in find_in_container(container, tag_q="scr_"):
            s = decode_script_chunk(ch.payload)
            if s["name"] in edits and s["name"] not in done:
                source = edits[s["name"]](lua_decompile(s["code"]))
                ch.set_payload(encode_script_chunk(s["name"], lua_compile(source)))
                done.add(s["name"])
        if not done:
            continue
        if done != set(edits):
            raise ValueError(f"{path}: no {', '.join(sorted(set(edits) - done))} script")
        rel = path.relative_to(src_dir)
        (out_dir / rel).parent.mkdir(parents=True, exist_ok=True)
        (out_dir / rel).write_bytes(container.raw_bytes())
        written.append(str(rel))
    if not written:
        raise ValueError(f"No common.lvl in {src_dir / 'Common'} has the pause menu scripts")
    core_lvl = parse_container((src_dir / "core.lvl").read_bytes())
    for _p, _i, ch, _n in find_in_container(core_lvl, tag_q="Locl"):
        strings = decode_locl(ch.payload)
        strings.set(DEBUG_MENU_LABEL_KEY, "CONSOLE")
        ch.set_payload(encode_locl(strings))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "core.lvl").write_bytes(core_lvl.raw_bytes())
    written.append("core.lvl")
    return written
