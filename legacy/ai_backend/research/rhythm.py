"""离线起音候选与教师参考拍点比较。"""

from .acoustics import frames, rms, samples


def rhythm_candidates(wav, config):
    mode = config.get("rhythm_mode")
    if mode == "free":
        return {"status": "abstained", "reason": "教师标为自由节奏，不比较拍点"}
    if mode != "marked":
        raise ValueError("rhythm_mode 须为 marked 或 free")
    reference = config.get("reference_hits_ms", [])
    if (not isinstance(reference, list)
            or any(type(time) is not int or time < 0 for time in reference)
            or any(left >= right for left, right in zip(reference, reference[1:]))):
        raise ValueError("参考拍点须为严格递增的原音轨整数毫秒")
    values, rate = samples(wav)
    length_ms = round(len(values) * 1000 / rate)
    if reference and reference[-1] > length_ms:
        raise ValueError("参考拍点超出样本范围")
    measured = [(time, rms(frame)) for time, frame in frames(values, rate, 20, 10)]
    if len(measured) < 8 or max(value for _, value in measured) < .02:
        return {"status": "abstained", "reason": "样本过短或信号过弱"}
    events = []
    for index in range(3, len(measured) - 2):
        time, amplitude = measured[index]
        recent = max(value for _, value in measured[index - 3:index])
        before = measured[index - 1][1]
        if amplitude < max(.02, recent * 1.6) or amplitude - before < .009:
            continue
        if not events or time - events[-1] >= 110:
            events.append(time)
    if not events:
        return {"status": "abstained", "reason": "未发现稳定的起音候选"}
    matches = []
    used = -1
    for expected in reference:
        available = range(used + 1, len(events))
        nearest = min(available, key=lambda index: abs(events[index] - expected), default=None)
        hit = None
        if nearest is not None and abs(events[nearest] - expected) <= 100:
            used = nearest
            hit = events[nearest]
        matches.append({"reference_ms": expected, "candidate_ms": hit,
                        "difference_ms": hit - expected if hit is not None else None})
    return {"status": "candidate", "onsets_ms": events, "matches": matches}
