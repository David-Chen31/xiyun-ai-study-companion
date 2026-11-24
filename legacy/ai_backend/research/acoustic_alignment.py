"""按人工锚点约束分段声学对齐，每段可独立弃判。"""

import math
from statistics import fmean

from .acoustics import rms, samples
from .alignment import anchor_map, validated_sections


STEP_MS = 20
SEARCH_BAND_MS = 260


def features(waveform, rate, start, end):
    times = list(range(start, end, STEP_MS)) + [end]
    values = []
    for stamp in times:
        first = min(max(0, round((stamp - 10) * rate / 1000)), len(waveform) - 1)
        last = max(first + 1, round((stamp + 10) * rate / 1000))
        frame = waveform[first:last]
        energy = math.log1p(40 * rms(frame))
        crossings = sum((a < 0) != (b < 0) for a, b in zip(frame, frame[1:])) / len(frame)
        values.append((energy, crossings))
    floor, ceiling = min(item[0] for item in values), max(item[0] for item in values)
    if ceiling - floor < .35:
        return times, []
    return times, [((energy - floor) / (ceiling - floor), crossings) for energy, crossings in values]


def constrained_path(left, right):
    rows, columns = len(left), len(right)
    if min(rows, columns) < 3 or not .5 <= (rows - 1) / (columns - 1) <= 2:
        return []
    steps = {(0, 0): (0, None)}
    radius = SEARCH_BAND_MS // STEP_MS
    for i, (a_energy, a_zero) in enumerate(left):
        center = round(i * (columns - 1) / (rows - 1))
        for j in range(max(0, center - radius), min(columns, center + radius + 1)):
            if not i or not j:
                continue
            previous = [(i - di, j - dj) for di, dj in ((1, 1), (1, 2), (2, 1))
                        if (i - di, j - dj) in steps]
            if not previous:
                continue
            b_energy, b_zero = right[j]
            distance = abs(a_energy - b_energy) + 0.25 * abs(a_zero - b_zero)
            parent = min(previous, key=lambda key: steps[key][0] + (.07 if sum(key) != i + j - 2 else 0))
            cost = steps[parent][0] + distance + (.07 if sum(parent) != i + j - 2 else 0)
            steps[i, j] = cost, parent
    point = (rows - 1, columns - 1)
    path = []
    while point in steps:
        path.append(point)
        point = steps[point][1]
    if len(path) < 3:
        return []
    distance = fmean(abs(left[i][0] - right[j][0]) for i, j in path)
    return list(reversed(path)) if distance <= .24 else []


def interpolate(path, time):
    if not path or not path[0][0] <= time <= path[-1][0]:
        return None
    for (a, b), (next_a, next_b) in zip(path, path[1:]):
        if time <= next_a:
            return round(b + (time - a) * (next_b - b) / (next_a - a))


def acoustic_alignment(data, left_wav, right_wav):
    left_id, right_id, sections, queries = validated_sections(data)
    left, left_rate = samples(left_wav)
    right, right_rate = samples(right_wav)
    duration_a, duration_b = round(len(left) * 1000 / left_rate), round(len(right) * 1000 / right_rate)
    if sections[-1][0][-1] > duration_a or sections[-1][1][-1] > duration_b:
        raise ValueError("锚点超出对应音轨")
    paths = []
    diagnostics = []
    for axis_a, axis_b in sections:
        for a0, a1, b0, b1 in zip(axis_a, axis_a[1:], axis_b, axis_b[1:]):
            times_a, a = features(left, left_rate, a0, a1)
            times_b, b = features(right, right_rate, b0, b1)
            path = [(times_a[i], times_b[j]) for i, j in constrained_path(a, b)]
            if path:
                paths.append(path)
            diagnostics.append({"left_ms": [a0, a1], "right_ms": [b0, b1],
                                "status": "candidate" if path else "abstained"})
    baseline = anchor_map(data)["mapping"]
    mappings = []
    for time, point in zip(queries, baseline):
        mapped = next((interpolate(path, time) for path in paths if path[0][0] <= time <= path[-1][0]), None)
        mappings.append({"left_ms": time, "right_ms": mapped, "anchor_right_ms": point["right_ms"]})
    return {"left_revision": left_id, "right_revision": right_id, "sections": diagnostics, "mapping": mappings}
