"""Local CREPE Tiny inference and optional prepared track references."""

import argparse
import json
import subprocess
from functools import lru_cache
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
MODEL_PATH = ROOT / ".xiyun" / "models" / "crepe-tiny.onnx"
ENGINE = "crepe-tiny"
RATE = 16_000
INPUT_RATE = 24_000
STEP_MS = 90
FIRST_MS = 28
FRAME_SIZE = 1024


@lru_cache(maxsize=1)
def model():
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = 3
    options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(MODEL_PATH), sess_options=options, providers=["CPUExecutionProvider"])
    return session, session.get_inputs()[0].name


def extract(pcm, sample_rate=INPUT_RATE):
    import numpy as np
    from scipy.signal import resample_poly

    signal = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    if sample_rate != RATE:
        signal = resample_poly(signal, 2, 3)
    padded = np.pad(signal, (FRAME_SIZE // 2, FRAME_SIZE // 2))
    centers = np.arange(FIRST_MS * RATE // 1000, signal.size, STEP_MS * RATE // 1000)
    session, input_name = model()
    points = []
    for start in range(0, centers.size, 96):
        times = centers[start:start + 96]
        frames = padded[times[:, None] + np.arange(FRAME_SIZE)]
        amplitude = np.sqrt(np.mean(frames ** 2, axis=1))
        salience = session.run(None, {input_name: frames})[0]
        for time, level, bins in zip(times, amplitude, salience):
            peak = int(np.argmax(bins))
            certainty = float(bins[peak])
            hz = None
            if level >= .004 and certainty >= .55:
                near = np.arange(max(0, peak - 4), min(360, peak + 5))
                cents = 1997.379408 + 20 * np.average(near, weights=bins[near])
                frequency = 10 * 2 ** (cents / 1200)
                if 65 <= frequency <= 1050:
                    hz = round(float(frequency), 1)
            db = round(float(20 * np.log10(max(level, .000032))))
            points.append([round(time * 1000 / RATE), hz, db])
    return points


def decode(path):
    return subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
        "-ar", str(RATE), "-f", "s16le", "pipe:1",
    ])


def enrich(path, vocals, mixed=None):
    profile = json.loads(path.read_text(encoding="utf-8"))
    if profile.get("contour_step_ms") != STEP_MS:
        raise ValueError("参考音高时间步长不匹配")
    reference = extract(decode(vocals), RATE)
    mix_reference = extract(decode(mixed), RATE) if mixed else None
    for line in profile["lines"]:
        start, end = line["start_ms"], line["end_ms"]
        line["model_reference"] = [point for point in reference if start <= point[0] < end]
        if mix_reference is not None:
            line["model_mix_reference"] = [point for point in mix_reference if start <= point[0] < end]
    profile["pitch_engine"] = ENGINE
    temp = path.with_suffix(".pitch.tmp")
    temp.write_text(json.dumps(profile, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    temp.replace(path)


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description="为曲目预制 CREPE Tiny 音高参考")
    cli.add_argument("profile", type=Path)
    cli.add_argument("vocals", type=Path)
    cli.add_argument("--mix", type=Path)
    arguments = cli.parse_args()
    enrich(arguments.profile, arguments.vocals, arguments.mix)
