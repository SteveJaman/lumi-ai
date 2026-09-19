import pyaudio

p = pyaudio.PyAudio()
print("Scanning available audio input devices...")
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    if info.get('maxInputChannels') > 0:
        print(f"Index {i}: {info.get('name')}")
p.terminate()