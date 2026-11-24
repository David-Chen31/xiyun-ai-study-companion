"""人工锚点分段线性对齐，间隔保留为空。"""

from bisect import bisect_right


def validated_sections(data):
    left, right = data.get("left_revision"), data.get("right_revision")
    groups, queries = data.get("sections"), data.get("query_ms")
    if (not isinstance(left, str) or not left or not isinstance(right, str) or not right or left == right
            or not isinstance(groups, list) or not groups
            or not isinstance(queries, list) or any(type(time) is not int or time < 0 for time in queries)):
        raise ValueError("需提供两个不同修订、独立锚点组和待查询原媒体整数毫秒")
    sections = []
    for section in groups:
        if (not isinstance(section, list) or len(section) < 2
                or any(not isinstance(pair, list) or len(pair) != 2
                       or any(type(value) is not int or value < 0 for value in pair) for pair in section)):
            raise ValueError("每个可比区至少要有两组整数锚点")
        a, b = tuple(zip(*section))
        if (any(first >= second for axis in (a, b) for first, second in zip(axis, axis[1:]))
                or (sections and (sections[-1][0][-1] >= a[0] or sections[-1][1][-1] >= b[0]))):
            raise ValueError("锚点和区间须严格递增，避免跨句和重叠")
        sections.append((a, b))
    return left, right, sections, queries


def anchor_map(data):
    left, right, sections, queries = validated_sections(data)
    mapped = []
    for time in queries:
        target = None
        for a, b in sections:
            if a[0] <= time <= a[-1]:
                index = min(bisect_right(a, time), len(a) - 1)
                ratio = (time - a[index - 1]) / (a[index] - a[index - 1])
                target = round(b[index - 1] + ratio * (b[index] - b[index - 1]))
                break
        mapped.append({"left_ms": time, "right_ms": target})
    return {"left_revision": left, "right_revision": right, "mapping": mapped}
