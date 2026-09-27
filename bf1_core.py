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

MAGIC = b"ucfb"

# --- FNV-1a 32-bit, matching the Zero Engine's string hashing ------------


def fnv1a32(value: str) -> int:
    h = 0x811C9DC5
    for ch in value.lower():
        h ^= ord(ch)
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
            from bf1_lua_decompile import decompile, LuaDecompileError
            source = decompile(script["code"])
        except Exception:
            # Decompilation is best-effort (see bf1_lua_decompile's module
            # docstring - this game's scripts have their local debug table
            # stripped, which breaks a chunk of real scripts). Fall back to
            # the raw bytecode rather than losing the export entirely.
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
        # A .lua file is decompiled *source text*, not bytecode, and there's
        # no Lua 4.0 compiler available here to turn it back into a loadable
        # chunk - writing its raw bytes into BODY would silently produce a
        # corrupt, non-executable script. Only the raw-bytecode formats
        # round-trip; .lua export (see export_chunk_to_file) is one-way.
        raise ValueError(
            "Can't import a .lua source file back into a scr_ chunk - there's no Lua 4.0 "
            "compiler available to turn source text into bytecode. Edit and re-export the "
            "original .luac bytecode instead.")
    if tag == "scr_" and suffix in (".luac", ".bin"):
        return encode_script_chunk(path.stem, path.read_bytes())
    if tag in KV_TAGS and suffix == ".odf":
        return decode_odf_text_to_kv_payload(path.read_text(encoding="ascii", errors="replace"),
                                             fallback_type=path.stem)
    data = path.read_bytes()
    if data[:4] == tag.encode("ascii", "replace") and len(data) >= 8:
        length = struct.unpack_from("<I", data, 4)[0]
        return data[8:8 + length]
    return data


# --- Script ("scr_") chunk codec -------------------------------------------
# Layout (found the same way as tex_ - exploding a scr_ chunk with
# swbf-unmunge.exe -mode explode):
#   NAME - null-terminated script name
#   INFO - single byte, always seen as 0x01 in this repo's sample data
#          (plausibly a format/version tag - not a length or count, there's
#          nothing else it could index)
#   BODY - the compiled script bytes verbatim. In every scr_ chunk sampled
#          here this is a standard Lua 5.1 bytecode chunk (starts with the
#          signature byte 0x1B + "Lua" + version byte 0x51), which
#          swbf-unmunge.exe itself only ever re-dumps as raw ".script"
#          bytes rather than decompiling - so a .luac dump (loadable by any
#          matching Lua 5.1 VM, or a bytecode decompiler such as unluac) is
#          already the most "usable" form available without writing a full
#          Lua bytecode decompiler.

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
# These are NOT key/value data (unlike entc above) - despite the display
# name suggesting a simple table, both use a fundamentally different binary
# format: a NAME block, then a 'BIN_' block whose payload is itself a tree
# of tag+length+payload blocks, but with each 4-byte tag stored BYTE-
# REVERSED compared to every other chunk type in this file (found by
# hexdumping: 'BIN_' contains bytes that read backwards as recognizable
# tags like 'ZAFF', 'VERS', 'SIZE', 'MTLS', 'SKEL', ...). swbf-unmunge.exe
# itself doesn't decode these into anything re-editable either - full-file
# extraction dumps them to a 'munged' folder as raw bytes, same fallback it
# uses for anything it can't fully unmunge. So rather than fake a KV editor
# for a format this module (and the external reference tool) only partly
# understands, decode_structured_binary_tree walks as far as the reversed-
# tag framing holds up and honestly reports whatever's left over as an
# opaque tail instead of guessing:
#  - zaf_'s BIN_ parses two full levels clean (verified: consumes all but a
#    2-byte alignment pad) - a single 'ZAFF' block wrapping VERS/SIZE/MTLS/
#    JSTS/SKNS/SKEL/PXYS/SHDS sub-blocks (materials/joints/skin/skeleton-
#    reference/proxy/shader tables for a rigged model).
#  - zaa_'s BIN_ only parses one small zero-length 'ANMS' marker before the
#    bytes stop looking like tag-framed data (the next 16 bytes don't
#    decode as a plausible tag+length) - that remainder is reported as an
#    unparsed raw tail rather than mis-rendered as fake structure.

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