"""专项样本的保守声学基线，所有时间为整数毫秒。"""

import math
import statistics
import sys
import wave
from array import array
from io import BytesIO


def samples(wav):
    try:
        with wave.open(BytesIO(wav), "rb") as source:
            rate = source.getframerate()
            frames = source.getnframes()
            if source.getsampwidth() != 2 or source.getnchannels() != 1 or not 4000 <= rate <= 48000:
                raise ValueError("研究样本须为 4–48 kHz 单声道 PCM16 WAV")
            if not 0 < frames <= rate * 20:
                raise ValueError("每个研究样本须在 20 秒以内")
            values = array("h", source.readframes(frames))
    except (wave.Error, EOFError) as error:
        raise ValueError("无法读取研究 WAV") from error
    if len(values) != frames:
        raise ValueError("研究音频已截断")
    if sys.byteorder != "little":
        values.byteswap()
    stride = max(1, rate // 8000)
    # 小窗平均可减轻直接隔点抽样带来的高频折叠。
    values = [sum(values[i:i + stride]) / (32768 * stride) for i in range(0, len(values) - stride + 1, stride)]
    return values, rate / stride


def frames(values, rate, size_ms, step_ms):
    size, step = round(rate * size_ms / 1000), round(rate * step_ms / 1000)
    for start in range(0, len(values) - size + 1, step):
        yield round(start * 1000 / rate), values[start:start + size]


def rms(frame):
    return math.sqrt(sum(value * value for value in frame) / len(frame))


def entry_parameters(config):
    if config.get("capture_route") != config.get("calibrated_route") or not config.get("calibrated_route"):
        raise ValueError("设备路由未校准或已变化")
    delay, origin = config.get("delay_ms"), config.get("reference_start_ms")
    speed, window = config.get("rate"), config.get("reference_window_ms")
    if not (type(delay) is int and 0 <= delay <= 1500 and type(origin) is int and origin >= 0
            and type(speed) in (int, float) and 0.5 <= speed <= 1.5
            and isinstance(window, list) and len(window) == 2
            and all(type(x) is int for x in window) and 0 <= window[0] <= window[1]):
        raise ValueError("缺少有效的延迟、参考时轴或教师窗口")
    return delay, origin, speed, window


def reference_entry(captured, timing):
    delay, origin, speed, (low, high) = timing
    timestamp = round(origin + speed * (captured - delay))
    if timestamp < 0 or captured < delay:
        return {"status": "abstained", "reason": "未发现可信的持续声音候选"}
    if min(abs(timestamp - low), abs(timestamp - high)) <= 20:
        return {"status": "abstained", "reason": "候选位置接近教师窗边界"}
    direction = "early" if timestamp < low else "late" if timestamp > high else "in_window"
    return {"status": "candidate", "captured_ms": captured, "reference_ms": timestamp, "relation": direction}


def entry_candidate(wav, config):
    try:
        timing = entry_parameters(config)
    except ValueError as error:
        return {"status": "abstained", "reason": str(error)}
    values, rate = samples(wav)
    clipped = sum(abs(value) >= .99 for value in values)
    if clipped > len(values) * .002:
        return {"status": "abstained", "reason": "信号削波"}
    energy = [(time, rms(frame)) for time, frame in frames(values, rate, 20, 10)]
    if len(energy) < 20:
        return {"status": "abstained", "reason": "样本太短"}
    baseline = max(level for _, level in energy[:10])
    threshold = max(.012, baseline * 4)
    if baseline >= .025:
        return {"status": "abstained", "reason": "开头缺少可用的安静窗口"}
    for index in range(10, len(energy) - 5):
        if all(level >= threshold for _, level in energy[index:index + 6]):
            return reference_entry(energy[index][0], timing)
    return {"status": "abstained", "reason": "未发现可信的持续声音候选"}


def pitch_candidate(frame, rate, *, min_rms=.015, min_similarity=.92):
    if rms(frame) < min_rms:
        return None
    center = statistics.fmean(frame)
    centered = [value - center for value in frame]
    low = max(2, round(rate / 600))
    high = min(round(rate / 80), len(frame) // 2)
    scores = [0.0]
    for lag in range(low - 1, high + 1):
        pairs = zip(centered, centered[lag:])
        dot = left = right = 0.0
        for a, b in pairs:
            dot += a * b
            left += a * a
            right += b * b
        scores.append(dot / math.sqrt(left * right) if left and right else 0)
    for i in range(2, len(scores) - 1):
        if scores[i] >= min_similarity and scores[i - 1] < scores[i] >= scores[i + 1]:
            lag = low - 2 + i
            return round(rate / lag, 1)
    return None


def voiced_entry_candidate(wav, config):
    """Find a sustained harmonic region, then look back for a weak initial consonant."""
    try:
        timing = entry_parameters(config)
    except ValueError as error:
        return {"status": "abstained", "reason": str(error)}
    values, rate = samples(wav)
    if sum(abs(value) >= .99 for value in values) > len(values) * .002:
        return {"status": "abstained", "reason": "信号削波"}
    energy = [(time, rms(frame)) for time, frame in frames(values, rate, 10, 5)]
    if not energy or energy[-1][0] < 200:
        return {"status": "abstained", "reason": "样本太短"}
    noise = sorted(level for time, level in energy if time < 90)
    noise_level = noise[int(.8 * len(noise))]
    if noise_level >= .025:
        return {"status": "abstained", "reason": "开头缺少可用的安静窗口"}
    voice_floor = max(.004, noise_level * 3)
    edge_floor = max(.0018, noise_level * 1.9)
    harmonic = []
    for stamp, frame in frames(values, rate, 35, 10):
        if stamp < 100:
            continue
        pitch = pitch_candidate(frame, rate, min_rms=voice_floor, min_similarity=.73)
        harmonic.append((stamp, pitch is not None))
        if len(harmonic) < 4 or not harmonic[-4][1] or sum(bit for _, bit in harmonic[-4:]) < 3:
            continue
        # A 35 ms window can catch the first cycles before its center reaches the onset.
        supported = harmonic[-4][0] + 10
        captured = supported
        gap = 0
        for time, level in reversed(energy):
            if time > supported:
                continue
            if time < max(90, supported - 160):
                break
            if level < edge_floor:
                gap += 5
                if gap > 15:
                    break
            else:
                captured = time
                gap = 0
        return reference_entry(captured, timing)
    return {"status": "abstained", "reason": "未发现持续的周期性声音"}


def pitch_track(wav):
    values, rate = samples(wav)
    return [{"time_ms": time + 20, "f0_hz": pitch_candidate(frame, rate)}
            for time, frame in frames(values, rate, 40, 80)]
