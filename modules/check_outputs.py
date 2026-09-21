"""List all audio OUTPUT devices with their current PyAudio indices.
Run this after any audio driver change (VB-Cable install, Windows update,
headset plug/unplug) to find the correct OUTPUT_DEVICE_INDEX values."""
import pyaudio

p = pyaudio.PyAudio()
print("\n=== Audio OUTPUT devices ===\n")
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if info.get('maxOutputChannels') > 0:
        name = info.get('name')
        chans = info.get('maxOutputChannels')
        rate = int(info.get('defaultSampleRate', 0))
        print(f"Index {i:>3}: {name}  [{chans} ch @ {rate} Hz]")
p.terminate()
print("\nLook for:")
print("  - 'CABLE Input' (VB-Audio Virtual Cable)  ← for VTube Studio")
print("  - your headset / speakers                  ← for your ears")
print("Put those two indices in OUTPUT_DEVICE_INDEX in constants.py\n")