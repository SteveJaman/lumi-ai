import asyncio
import math
from vts_manager import VTSManager


FPS = 30
DT = 1.0 / FPS


async def phase(duration_s, label, fn):
    print(f"\n--- {label} ---")
    frames = int(duration_s * FPS)
    for t in range(frames):
        fn(t)
        await asyncio.sleep(DT)
    print(f"    done ({duration_s}s)")


async def main():
    vts = VTSManager(config_path="./params.json")

    try:
        await vts.connect_and_register()

        required = {
            "FaceAngleX", "FaceAngleY", "FaceAngleZ",
            "EyeRightX", "EyeRightY",
            "EyeOpenLeft", "EyeOpenRight",
            "MouthOpen", "MouthSmile",
        }
        have = vts.existing_params | vts.created_params
        missing = required - have
        if missing:
            print(f"\n!! Missing params on this model: {sorted(missing)}")
            print("   Check VTS → Settings → Model → Parameter Settings.")
            return

        vts.start_streaming(fps=FPS)
        await asyncio.sleep(0.3)

        # ---- Phase 1: head sweep ----
        await phase(3.0, "Phase 1 — head sweep", lambda t: (
            vts.set_parameter("FaceAngleX", 30.0 * math.sin(t * 0.05)),
            vts.set_parameter("FaceAngleY", 20.0 * math.sin(t * 0.035)),
            vts.set_parameter("FaceAngleZ", 15.0 * math.sin(t * 0.07)),
        ))

        # ---- Phase 2: eye darting ----
        await phase(3.0, "Phase 2 — eye darting", lambda t: (
            vts.set_parameter("FaceAngleX", 0.0),
            vts.set_parameter("FaceAngleY", 0.0),
            vts.set_parameter("FaceAngleZ", 0.0),
            vts.set_parameter("EyeRightX", 0.9 * math.sin(t * 0.20)),
            vts.set_parameter("EyeRightY", 0.7 * math.sin(t * 0.13)),
        ))

        # ---- Phase 3: blink + mouth ----
        def blink_mouth(t):
            blink_t = t % int(FPS * 1.2)
            eye = 0.0 if blink_t < 5 else 1.0
            vts.set_parameter("EyeOpenLeft", eye)
            vts.set_parameter("EyeOpenRight", eye)
            vts.set_parameter("MouthOpen", abs(math.sin(t * 0.25)))
            vts.set_parameter("EyeRightX", 0.0)
            vts.set_parameter("EyeRightY", 0.0)

        await phase(4.0, "Phase 3 — blink + mouth", blink_mouth)

        # ---- Phase 4: smile sweep ----
        def smile(t):
            vts.set_parameter("EyeOpenLeft", 1.0)
            vts.set_parameter("EyeOpenRight", 1.0)
            vts.set_parameter("MouthOpen", 0.0)
            f = t / (FPS * 3.0)
            vts.set_parameter("MouthSmile", math.sin(f * math.pi))

        await phase(3.0, "Phase 4 — smile sweep", smile)

        # ---- Phase 5: full combo ----
        def combo(t):
            vts.set_parameter("FaceAngleX", 25.0 * math.sin(t * 0.05))
            vts.set_parameter("FaceAngleY", 15.0 * math.sin(t * 0.03))
            vts.set_parameter("FaceAngleZ", 12.0 * math.sin(t * 0.06))
            vts.set_parameter("EyeRightX", 0.6 * math.sin(t * 0.18))
            vts.set_parameter("EyeRightY", 0.5 * math.sin(t * 0.11))

            blink_t = t % int(FPS * 1.6)
            eye = 0.0 if blink_t < 5 else 1.0
            vts.set_parameter("EyeOpenLeft", eye)
            vts.set_parameter("EyeOpenRight", eye)

            vts.set_parameter("MouthOpen", abs(math.sin(t * 0.22)))
            vts.set_parameter("MouthSmile", 0.5 + 0.4 * math.sin(t * 0.04))

        await phase(5.0, "Phase 5 — full combo", combo)

        print("\nTest complete.")

    finally:
        await vts.close()


if __name__ == "__main__":
    asyncio.run(main())