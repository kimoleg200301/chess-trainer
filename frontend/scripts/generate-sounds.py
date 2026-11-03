"""Build local PCM effects. Requires ffmpeg for the CC0 chess recording.

Source, license and processing notes: sources/chess-piece-setting.LICENSE.txt.
The source is bundled; rebuilding never needs a network request.
"""
import hashlib
import math
from pathlib import Path
import struct
import subprocess
import wave

folder = Path(__file__).resolve().parents[1] / 'src' / 'sounds'
folder.mkdir(exist_ok=True)
rate = 22050


def write(name, samples):
    assert max(abs(value) for value in samples) < 32767
    with wave.open(str(folder / (name + '.wav')), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))


source = Path(__file__).parent / 'sources' / 'chess-piece-setting.mp3'
assert hashlib.sha256(source.read_bytes()).hexdigest() == '89ff2c5ba4db9d40f15cc81332a895bdbb386f29266edd9f159adb1339eed6bb'
decoded = subprocess.check_output([
    'ffmpeg', '-v', 'error', '-i', str(source), '-af',
    'atrim=start=2.535:duration=0.28,asetpts=PTS-STARTPTS,highpass=f=80,afade=t=in:d=0.001,afade=t=out:st=0.24:d=0.04',
    '-ac', '1', '-ar', str(rate), '-f', 'f32le', '-',
])
tap = struct.unpack('<' + 'f' * (len(decoded) // 4), decoded)
scale = .72 * 32767 / max(abs(value) for value in tap)
move = [round(value * scale) for value in tap]
write('move', move)
# A softer contact followed by the placement, retaining the recording's pitch.
offset = round(.09 * rate)
capture = [0] * (len(move) + offset)
for index, value in enumerate(move):
    capture[index] += round(value * .45)
    capture[index + offset] += value
write('capture', capture)

for name, duration in [('wrong', .32), ('complete', .85)]:
    samples = []
    for i in range(round(rate * duration)):
        t = i / rate
        notes = [523.25, 659.25, 783.99] if name == 'complete' else [293.66, 246.94]
        value = 0
        for index, frequency in enumerate(notes):
            age = t - index * (.14 if name == 'complete' else .11)
            if age >= 0:
                value += .28 * math.sin(2 * math.pi * frequency * age) * min(1, age / .008) * math.exp(-age * (9 if name == 'complete' else 22))
        value *= min(1, (duration - t) / .02)
        assert abs(value) < 1
        samples.append(round(value * 32767))
    write(name, samples)
