"""Generate the trainer's original PCM effects. No external audio or dependencies."""
import math
from pathlib import Path
import random
import struct
import wave

folder = Path(__file__).resolve().parents[1] / 'src' / 'sounds'
folder.mkdir(exist_ok=True)
rate = 22050
for name, duration in [('move', .18), ('capture', .22), ('wrong', .32), ('complete', .85)]:
    samples = []
    noise = random.Random(7)
    for i in range(round(rate * duration)):
        t = i / rate
        if name in ('move', 'capture'):
            frequency = 620 if name == 'move' else 430
            value = min(1, t / .002) * (
                .55 * math.sin(2 * math.pi * frequency * t) * math.exp(-t * 32)
                + .18 * math.sin(2 * math.pi * frequency * 1.73 * t) * math.exp(-t * 55)
                + .14 * noise.uniform(-1, 1) * math.exp(-t * 95))
        else:
            notes = [523.25, 659.25, 783.99] if name == 'complete' else [293.66, 246.94]
            value = 0
            for index, frequency in enumerate(notes):
                age = t - index * (.14 if name == 'complete' else .11)
                if age >= 0:
                    value += .28 * math.sin(2 * math.pi * frequency * age) * min(1, age / .008) * math.exp(-age * (9 if name == 'complete' else 22))
        value *= min(1, (duration - t) / .02)
        assert abs(value) < 1
        samples.append(round(value * 32767))
    with wave.open(str(folder / (name + '.wav')), 'wb') as output:
        output.setnchannels(1); output.setsampwidth(2); output.setframerate(rate)
        output.writeframes(struct.pack('<' + 'h' * len(samples), *samples))
