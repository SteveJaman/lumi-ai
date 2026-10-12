"""
Standalone test harness for the VTS motion subsystem.

Opens its own pyvts connection (separate plugin name and token path,
so it won't interfere with the running Lumi instance) and drives each
motion channel through a scripted sequence with pauses so you can
visually confirm what works and how much range each param has.

First run will pop an "Allow plugin?" dialog in VTS — click Allow.
Subsequent runs reuse the saved token.

IMPORTANT — VTS Input names vs Live2D params
--------------------------------------------
VTS silently accepts injection into Live2D parameter names (like
ParamAngleX) and then drops the value on the floor. Only VTS *Input*
names (the "tracking parameters" in the model's ParameterSettings
block of ariu.vtube.json) actually get applied.

So every candidate list below puts VTS Input names FIRST and Live2D
parameter names second as a fallback for other models. The probe
walks the list and picks the first name VTS accepts — if a Live2D
name comes first, the probe stops there and everything silently
fails.

The Input names for this model are, from ariu.vtube.json:
    FaceAngleX/Y/Z    → head + body angles
    EyeOpenLeft/Right → eye open
    EyeRightX/Y       → gaze (EyeRightX is X-inverted in the mapping)
    MouthOpen         → mouth open
    MouthSmile        → mouth form
    Brows             → all four brow params (shared input)

Usage:
    python tests/test_vtube_motion.py                # interactive
    python tests/test_vtube_motion.py --fast         # no Enter prompts
    python tests/test_vtube_motion.py --only head    # single channel
    python tests/test_vtube_motion.py --duration 30  # longer idle
    python tests/test_vtube_motion.py --list-params  # dump param list

Channels: head, eyes, blink, brows, mouth, body, idle
"""

import argparse
import asyncio
import math
import os
import sys
import time

import pyvts


# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------
TOKEN_PATH = os.path.join("data", "vtubeStudio_test_token.txt")

# Candidate names. VTS Input names FIRST, Live2D params as fallback.
# See module docstring for why order matters.
HEAD_CANDIDATES = {
    "x":  ["FaceAngleX",   "ParamAngleX",   "AngleX"],
    "y":  ["FaceAngleY",   "ParamAngleY",   "AngleY"],
    "z":  ["FaceAngleZ",   "ParamAngleZ",   "AngleZ"],
    "el": ["EyeOpenLeft",  "ParamEyeLOpen"],
    "er": ["EyeOpenRight", "ParamEyeROpen"],
}

EXTRA_CANDIDATES = {
    "ex":  ["EyeRightX", "EyeLeftX", "ParamEyeBallX"],
    "ey":  ["EyeRightY", "EyeLeftY", "ParamEyeBallY"],
    "bx":  ["FaceAngleX", "ParamBodyAngleX", "BodyAngleX"],
    "by":  ["FaceAngleY", "ParamBodyAngleY", "BodyAngleY"],
    "bz":  ["FaceAngleZ", "ParamBodyAngleZ", "BodyAngleZ"],
    "bly": ["Brows", "ParamBrowLY"],
    "bry": ["Brows", "ParamBrowRY"],
    "blf": ["Brows", "ParamBrowLForm"],
    "brf": ["Brows", "ParamBrowRForm"],
}

MOUTH_CANDIDATES = ["MouthOpen", "ParamMouthOpenY"]

# VTS Input fallbacks — used only if a direct param name was tried
# first and rejected. With the reorder above, this should stay unused
# on ariu, but it's kept for models that expose Live2D params as
# injectable.
VTS_INPUT_FALLBACK = {
    "bx":  "FaceAngleX",
    "by":  "FaceAngleY",
    "bz":  "FaceAngleZ",
    "bly": "Brows",
    "bry": "Brows",
    "blf": "Brows",
    "brf": "Brows",
}

# Clamp ranges. VTS Input names and Live2D param names can have
# different expected ranges (Brows is 0..1, ParamBrowLY is -1..1),
# so both are listed here.
RANGES = {
    # Head — both input and Live2D param share the -30..30 range
    "ParamAngleX": (-30, 30), "ParamAngleY": (-30, 30), "ParamAngleZ": (-30, 30),
    "FaceAngleX":  (-30, 30), "FaceAngleY":  (-30, 30), "FaceAngleZ":  (-30, 30),

    # Eyes
    "ParamEyeLOpen":  (0, 1), "ParamEyeROpen":  (0, 1),
    "EyeOpenLeft":    (0, 1), "EyeOpenRight":   (0, 1),
    "ParamEyeBallX": (-1, 1), "ParamEyeBallY": (-1, 1),
    "EyeRightX":     (-1, 1), "EyeRightY":     (-1, 1),
    "EyeLeftX":      (-1, 1), "EyeLeftY":      (-1, 1),

    # Body
    "ParamBodyAngleX": (-10, 10),
    "ParamBodyAngleY": (-10, 10),
    "ParamBodyAngleZ": (-10, 10),
    "BodyAngleX":      (-10, 10),
    "BodyAngleY":      (-10, 10),
    "BodyAngleZ":      (-10, 10),

    # Brows — shared VTS input is 0..1, direct Live2D params are -1..1
    "ParamBrowLY":    (-1, 1), "ParamBrowRY":    (-1, 1),
    "ParamBrowLForm": (-1, 1), "ParamBrowRForm": (-1, 1),
    "Brows":          (0, 1),

    # Mouth
    "MouthOpen":       (0, 1),
    "ParamMouthOpenY": (0, 2.1),
    "MouthSmile":      (-1, 1),
    "ParamMouthForm":  (-1, 1),
}


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------
def clamp(pid, value):
    lo, hi = RANGES.get(pid, (-1, 1))
    return max(lo, min(hi, float(value)))


async def send_param(vts, pid, value, mode="set"):
    """Inject a single param. Returns response dict, or None on error."""
    req = vts.vts_request.BaseRequest(
        "InjectParameterDataRequest",
        {
            "faceFound": False,
            "mode": mode,
            "parameterValues": [
                {"id": pid, "value": clamp(pid, value), "weight": 1.0},
            ],
        },
    )
    try:
        return await vts.request(req)
    except Exception as e:
        print(f"      !! injection error: {e}")
        return None


async def probe(vts, candidates, value=0.0, mode="set"):
    """Try each candidate name; return first that VTS accepts."""
    for name in candidates:
        resp = await send_param(vts, name, value, mode)
        if resp and resp.get("messageType") != "APIError":
            return name
    return None


async def wait_for_enter(msg=""):
    if msg:
        print(f"\n  ▸ {msg}")
    try:
        await asyncio.get_event_loop().run_in_executor(
            None, input, "  [Enter] "
        )
    except (EOFError, RuntimeError):
        pass


async def reset_all(vts, names):
    """Return every verified param to its neutral value."""
    for slot, pid in names["head"].items():
        neutral = 1.0 if slot in ("el", "er") else 0.0
        await send_param(vts, pid, neutral)
    for slot, pid in names["extra"].items():
        neutral = 0.5 if pid == "Brows" else 0.0
        await send_param(vts, pid, neutral)
    if names["mouth"]:
        await send_param(vts, names["mouth"], 0.0)


# --------------------------------------------------------------------
# Connection + discovery
# --------------------------------------------------------------------
async def connect_vts():
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    plugin_info = {
        "plugin_name": "Lumi VTS Test Harness",
        "developer": "Lumi AI",
        "authentication_token_path": TOKEN_PATH,
    }
    vts = pyvts.vts(plugin_info=plugin_info)

    try:
        await vts.connect()
    except Exception as e:
        print(f"✗ Could not connect to VTS: {e}")
        print("  Check that VTube Studio is running and the API is enabled.")
        return None

    print("\n  [First run only] VTS will ask you to allow the plugin.")
    print("  Click 'Allow' in the VTS popup.\n")
    try:
        await vts.request_authenticate_token()
        await vts.request_authenticate()
    except Exception as e:
        print(f"✗ Authentication failed: {e}")
        try:
            await vts.close()
        except Exception:
            pass
        return None

    print("✓ Connected and authenticated.")
    return vts


async def check_connection(vts):
    print("\n=== Connection ===")
    req = vts.vts_request.requestHotKeyList()
    resp = await vts.request(req)
    if not resp or resp.get("messageType") == "APIError":
        print("  ✗ API handshake failed")
        return False
    hotkeys = resp.get("data", {}).get("availableHotkeys", [])
    names = [hk.get("name") for hk in hotkeys if hk.get("name")]
    print(f"  ✓ {len(hotkeys)} hotkeys available")
    if names:
        print(f"    {sorted(names)}")
    return True


async def discover_names(vts):
    print("\n=== Parameter discovery ===")
    names = {"head": {}, "extra": {}, "mouth": None}

    print("  head:")
    for slot, candidates in HEAD_CANDIDATES.items():
        name = await probe(vts, candidates)
        if name:
            names["head"][slot] = name
            print(f"    {slot:3s} → {name}")
        else:
            print(f"    {slot:3s} → (unavailable)")

    print("  extra:")
    for slot, candidates in EXTRA_CANDIDATES.items():
        name = await probe(vts, candidates)
        if name:
            names["extra"][slot] = name
            print(f"    {slot:3s} → {name}")
            continue
        fallback = VTS_INPUT_FALLBACK.get(slot)
        if fallback:
            fb = await probe(vts, [fallback])
            if fb:
                names["extra"][slot] = fb
                print(f"    {slot:3s} → {fb}  (fallback, shared VTS input)")
                continue
        print(f"    {slot:3s} → (unavailable)")

    print("  mouth:")
    name = await probe(vts, MOUTH_CANDIDATES)
    if name:
        names["mouth"] = name
        print(f"    → {name}")
    else:
        print(f"    → (unavailable)")

    return names


async def list_params(vts):
    print("\n=== Model parameter list ===")
    req = {
        "apiName": "VTubeStudioPublicAPI",
        "apiVersion": "1.0",
        "requestID": "TestParamList",
        "messageType": "Live2DParameterListRequest",
        "data": {},
    }
    resp = await vts.request(req)
    if not resp or resp.get("messageType") == "APIError":
        print("  ✗ could not fetch param list")
        return
    params = resp.get("data", {}).get("parameters", [])
    print(f"  {len(params)} params:")
    for p in sorted(params, key=lambda x: x.get("name", "")):
        name = p.get("name", "?")
        vmin = p.get("min", 0.0)
        vmax = p.get("max", 0.0)
        vdef = p.get("default", 0.0)
        print(f"    {name:30s}  min={vmin:+7.3f}  max={vmax:+7.3f}  default={vdef:+7.3f}")


# --------------------------------------------------------------------
# Channel tests
# --------------------------------------------------------------------
async def test_head(vts, names, step=0.5):
    print("\n=== Head sweep ===")
    h = names["head"]
    if "x" not in h or "y" not in h or "z" not in h:
        print("  incomplete head detection — skipping")
        return

    async def put(hx, hy, hz):
        await send_param(vts, h["x"], hx)
        await send_param(vts, h["y"], hy)
        await send_param(vts, h["z"], hz)

    print("  X sweep: left → right → center")
    for v in (-20, 0, 20, 0, -20, 0):
        await put(v, 0, 0)
        await asyncio.sleep(step)

    print("  Y sweep: up → down → center")
    for v in (20, 0, -15, 0, 20, 0):
        await put(0, v, 0)
        await asyncio.sleep(step)

    print("  Z tilt: left → right → center")
    for v in (-25, 0, 25, 0):
        await put(0, 0, v)
        await asyncio.sleep(step)

    print("  combined diagonal")
    for x, y, z in [(15, 12, -10), (-15, -12, 10), (0, 0, 0)]:
        await put(x, y, z)
        await asyncio.sleep(step)

    await put(0, 0, 0)


async def test_eyes(vts, names, step=0.5):
    print("\n=== Eye saccades ===")
    e = names["extra"]
    if "ex" not in e or "ey" not in e:
        print("  gaze params unavailable — skipping")
        return

    seq = [
        ("center",   0.0,  0.0),
        ("far left", -0.9, 0.0),
        ("far right", 0.9, 0.0),
        ("up",        0.0, 0.7),
        ("down",      0.0,-0.6),
        ("upper-left",-0.7, 0.6),
        ("lower-right",0.7,-0.5),
        ("center",    0.0, 0.0),
    ]
    for label, x, y in seq:
        await send_param(vts, e["ex"], x)
        await send_param(vts, e["ey"], y)
        print(f"  gaze → {label:12s} ({x:+.2f}, {y:+.2f})")
        await asyncio.sleep(step)

    await send_param(vts, e["ex"], 0.0)
    await send_param(vts, e["ey"], 0.0)


async def test_blink(vts, names, count=4):
    print(f"\n=== Blink test ({count} blinks) ===")
    h = names["head"]
    if "el" not in h or "er" not in h:
        print("  eye-open params unavailable — skipping")
        return

    for i in range(count):
        await send_param(vts, h["el"], 0.0)
        await send_param(vts, h["er"], 0.0)
        await asyncio.sleep(0.08)
        await send_param(vts, h["el"], 1.0)
        await send_param(vts, h["er"], 1.0)
        print(f"  blink {i + 1}/{count}")
        await asyncio.sleep(0.6)


async def test_brows(vts, names, step=0.7):
    print("\n=== Brow emotions ===")
    e = names["extra"]
    if not any(k in e for k in ("bly", "bry", "blf", "brf")):
        print("  brow params unavailable — skipping")
        return

    presets = [
        ("neutral",   0.00,  0.00,  0.00,  0.00),
        ("happy",     0.15,  0.15,  0.10,  0.10),
        ("surprised", 0.75,  0.75,  0.25,  0.25),
        ("sad",      -0.40, -0.40,  0.35,  0.35),
        ("angry",    -0.55, -0.55, -0.65, -0.65),
        ("confused", -0.15,  0.35, -0.20,  0.10),
        ("neutral",   0.00,  0.00,  0.00,  0.00),
    ]

    for label, vly, vry, vlf, vrf in presets:
        # Brows shared input expects 0..1, so remap [-1, 1] → [0, 1]
        if "bly" in e:
            v = (vly + 1.0) * 0.5 if e["bly"] == "Brows" else vly
            await send_param(vts, e["bly"], v)
        if "bry" in e:
            v = (vry + 1.0) * 0.5 if e["bry"] == "Brows" else vry
            await send_param(vts, e["bry"], v)
        if "blf" in e:
            v = (vlf + 1.0) * 0.5 if e["blf"] == "Brows" else vlf
            await send_param(vts, e["blf"], v)
        if "brf" in e:
            v = (vrf + 1.0) * 0.5 if e["brf"] == "Brows" else vrf
            await send_param(vts, e["brf"], v)
        print(f"  {label:10s}  LY={vly:+.2f}  RY={vry:+.2f}  LF={vlf:+.2f}  RF={vrf:+.2f}")
        await asyncio.sleep(step)

    # Reset
    for slot in ("bly", "bry", "blf", "brf"):
        if slot in e:
            neutral = 0.5 if e[slot] == "Brows" else 0.0
            await send_param(vts, e[slot], neutral)


async def test_mouth(vts, names, cycles=4):
    print(f"\n=== Mouth envelope ({cycles} cycles) ===")
    pid = names.get("mouth")
    if not pid:
        print("  mouth param unavailable — skipping")
        return

    for i in range(cycles):
        for v in (0.2, 0.5, 0.9, 0.6, 0.3, 0.1, 0.0):
            await send_param(vts, pid, v)
            await asyncio.sleep(0.07)
        await asyncio.sleep(0.15)
        print(f"  cycle {i + 1}/{cycles}")


async def test_body(vts, names, step=0.6):
    print("\n=== Body sway ===")
    e = names["extra"]
    if not any(k in e for k in ("bx", "by", "bz")):
        print("  body params not injectable (expected on ariu) — skipping")
        return

    # If body fell back to head inputs, note the coupling up front
    coupled = e.get("bx") == "FaceAngleX"
    if coupled:
        print("  NOTE: body shares head's VTS input — moving body will also move head")

    seq = [
        ("center",    0,   0,   0),
        ("lean left", 8,   0,   0),
        ("lean right",-8,   0,   0),
        ("forward",   0,   6,   0),
        ("back",      0,  -6,   0),
        ("tilt L",    0,   0,   8),
        ("tilt R",    0,   0,  -8),
        ("center",    0,   0,   0),
    ]
    for label, bx, by, bz in seq:
        if "bx" in e: await send_param(vts, e["bx"], bx)
        if "by" in e: await send_param(vts, e["by"], by)
        if "bz" in e: await send_param(vts, e["bz"], bz)
        print(f"  {label}")
        await asyncio.sleep(step)


async def test_idle_combo(vts, names, duration=15.0):
    print(f"\n=== Combined idle ({duration:.0f}s @ 30 Hz) ===")
    print("  Watch for: head sway, body follow, eye saccades, blinks, brow noise")

    h = names["head"]
    e = names["extra"]
    if not h:
        print("  no head params — skipping")
        return

    start = time.time()
    next_blink = start + 2.0
    frame = 1.0 / 30.0

    while time.time() - start < duration:
        t = time.time() - start

        # Head: layered sines at the same rates as the controller
        hx = (math.sin(t * 0.5) + 0.5 * math.sin(t * 1.1 + 1.0)) * 6.0
        hy = (math.sin(t * 0.4 + 2.0) + 0.4 * math.sin(t * 0.9)) * 5.0
        hz = math.sin(t * 0.6 + 0.5) * 3.0

        if "x" in h: await send_param(vts, h["x"], hx)
        if "y" in h: await send_param(vts, h["y"], hy)
        if "z" in h: await send_param(vts, h["z"], hz)

        # Eyes: continuous drift (test only — the real controller
        # does discrete saccades, but drift is easier to visually track)
        if "ex" in e:
            await send_param(vts, e["ex"], math.sin(t * 0.4) * 0.5)
        if "ey" in e:
            await send_param(vts, e["ey"], math.sin(t * 0.3 + 1.0) * 0.3)

        # Brows: idle noise
        if "bly" in e:
            v = math.sin(t * 0.17) * 0.03
            if e["bly"] == "Brows": v = (v + 1.0) * 0.5
            await send_param(vts, e["bly"], v)
        if "bry" in e:
            v = math.sin(t * 0.17 + 0.3) * 0.03
            if e["bry"] == "Brows": v = (v + 1.0) * 0.5
            await send_param(vts, e["bry"], v)

        # Blink
        now = time.time()
        if now >= next_blink:
            if "el" in h: await send_param(vts, h["el"], 0.0)
            if "er" in h: await send_param(vts, h["er"], 0.0)
            await asyncio.sleep(0.08)
            if "el" in h: await send_param(vts, h["el"], 1.0)
            if "er" in h: await send_param(vts, h["er"], 1.0)
            next_blink = now + 3.0

        await asyncio.sleep(frame)

    print("  done")


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------
async def main_async(args):
    print("=" * 62)
    print("  VTS Motion Test Harness")
    print("=" * 62)

    vts = await connect_vts()
    if not vts:
        return 1

    try:
        if not await check_connection(vts):
            return 1

        if args.list_params:
            await list_params(vts)
            return 0

        names = await discover_names(vts)

        print("\n=== Discovered channels ===")
        for slot, pid in names["head"].items():
            print(f"  head.{slot:3s} → {pid}")
        for slot, pid in names["extra"].items():
            print(f"  extra.{slot:3s} → {pid}")
        if names["mouth"]:
            print(f"  mouth     → {names['mouth']}")

        if not args.fast:
            await wait_for_enter(
                "Starting tests. Watch the model and press Enter between stages."
            )

        only = args.only

        async def stage(channel, coro):
            if only not in ("all", channel):
                return
            await coro
            if not args.fast:
                await wait_for_enter("")

        await stage("head",  test_head(vts, names))
        await stage("eyes",  test_eyes(vts, names))
        await stage("blink", test_blink(vts, names))
        await stage("brows", test_brows(vts, names))
        await stage("mouth", test_mouth(vts, names))
        await stage("body",  test_body(vts, names))

        if only in ("all", "idle"):
            await test_idle_combo(vts, names, duration=args.duration)

        # Reset everything before disconnect
        await reset_all(vts, names)

        print("\n" + "=" * 62)
        print("  Test run complete.")
        print("=" * 62)
        return 0
    finally:
        try:
            await vts.close()
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(
        description="Test each VTS motion channel in isolation."
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Skip the Enter prompts between stages.",
    )
    parser.add_argument(
        "--only",
        choices=["all", "head", "eyes", "blink", "brows", "mouth", "body", "idle"],
        default="all",
        help="Run only one channel.",
    )
    parser.add_argument(
        "--duration", type=float, default=15.0,
        help="Duration of the combined idle test, in seconds.",
    )
    parser.add_argument(
        "--list-params", action="store_true",
        help="Just dump the model's Live2D param list and exit.",
    )
    args = parser.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n  interrupted.")
        return 130


if __name__ == "__main__":
    sys.exit(main())