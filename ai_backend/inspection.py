"""WAV 实测与句界检查。

规则来自 docs/team/API_CONTRACT.md：
- 文件 ≤100 MB、时长 ≤10 分钟、16-bit PCM WAV、单/双声道；
- duration_ms 由实际帧数换算（向下取整到整数毫秒）；
- 每句 0 ≤ lead_ms ≤ start_ms < end_ms ≤ duration_ms；
- 非空 entry_ms 位于 [lead_ms, end_ms)；
- 拒绝重复句 ID、空数组和截断文件。

只读输入，全程在内存中处理，不写临时文件，无发布副作用。
"""

from __future__ import annotations

import io
import json
import math
import struct
import wave

from . import PROCESSOR_VERSION

MAX_FILE_BYTES = 100 * 1024 * 1024  # 100 MB
MAX_DURATION_MS = 10 * 60 * 1000    # 10 分钟
MAX_METADATA_BYTES = 256 * 1024


class InspectError(Exception):
    """带 HTTP 状态码与契约错误形状的业务错误。"""

    def __init__(self, status: int, code: str, message: str, field: str | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.field = field

    def to_body(self, request_id: str | None) -> dict:
        body = {"request_id": request_id, "code": self.code, "message": self.message}
        if self.field is not None:
            body["field"] = self.field
        return body


def _bad_input(message: str, field: str | None = None) -> InspectError:
    return InspectError(400, "INVALID_INPUT", message, field)


def _bad_boundary(message: str, field: str) -> InspectError:
    return InspectError(422, "INVALID_BOUNDARY", message, field)


def _is_int(value) -> bool:
    # bool 是 int 的子类，契约要求整数毫秒，显式排除布尔。
    return isinstance(value, int) and not isinstance(value, bool)


def decode_metadata(raw: bytes):
    """Bound JSON size and reject ambiguous duplicate keys/non-finite values."""
    if len(raw) > MAX_METADATA_BYTES:
        raise InspectError(413, "TOO_LARGE", "metadata 超过 256 KiB", "metadata")

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("重复 JSON 字段")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("不接受非有限 JSON 数值")

    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_constant=reject_constant)
        stack = [(data, 1)]
        while stack:
            value, depth = stack.pop()
            if isinstance(value, (dict, list)):
                if depth > 64:
                    raise ValueError("JSON 嵌套超过 64 层")
                if isinstance(value, dict):
                    for key in value:
                        key.encode("utf-8")
                    children = value.values()
                else:
                    children = value
                stack.extend((child, depth + 1) for child in children)
            elif isinstance(value, str):
                value.encode("utf-8")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("JSON 数值超出有限范围")
        return data
    except (UnicodeError, ValueError, RecursionError) as error:
        raise _bad_input("metadata 必须是有效 UTF-8 JSON，且不能含重复字段或非有限数值", "metadata") from error


def validate_metadata(metadata) -> tuple[str, str, list]:
    """校验 metadata 的结构与字段类型（400 级），返回 (request_id, revision_id, phrases)。"""
    if not isinstance(metadata, dict):
        raise _bad_input("metadata 必须是 JSON 对象", "metadata")
    request_id = metadata.get("request_id")
    revision_id = metadata.get("revision_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise _bad_input("request_id 必须是非空字符串", "request_id")
    if not isinstance(revision_id, str) or not revision_id.strip():
        raise _bad_input("revision_id 必须是非空字符串", "revision_id")
    phrases = metadata.get("phrases")
    if not isinstance(phrases, list) or not phrases:
        raise _bad_input("phrases 必须是非空数组", "phrases")
    seen_ids: set[str] = set()
    for i, phrase in enumerate(phrases):
        prefix = f"phrases[{i}]"
        if not isinstance(phrase, dict):
            raise _bad_input("句子必须是 JSON 对象", prefix)
        pid = phrase.get("id")
        if not isinstance(pid, str) or not pid.strip():
            raise _bad_input("句 id 必须是非空字符串", f"{prefix}.id")
        if pid in seen_ids:
            raise _bad_boundary("句 ID 重复", f"{prefix}.id")
        seen_ids.add(pid)
        if not isinstance(phrase.get("text"), str):
            raise _bad_input("text 必须是字符串", f"{prefix}.text")
        for key in ("lead_ms", "start_ms", "end_ms"):
            if not _is_int(phrase.get(key)):
                raise _bad_input(f"{key} 必须是整数毫秒", f"{prefix}.{key}")
        if "entry_ms" not in phrase:
            raise _bad_input("缺少 entry_ms；无入点时请传 null", f"{prefix}.entry_ms")
        entry = phrase.get("entry_ms")
        if entry is not None and not _is_int(entry):
            raise _bad_input("entry_ms 必须是整数毫秒或 null", f"{prefix}.entry_ms")
    return request_id, revision_id, phrases


def _wav_data_size(audio_bytes: bytes) -> int:
    """先检查 RIFF 容器，避免 wave 对半个采样帧向下取整而漏掉坏包。"""
    def malformed(message: str):
        return InspectError(415, "UNSUPPORTED_AUDIO", message, "audio")

    if len(audio_bytes) < 12 or audio_bytes[:4] != b"RIFF" or audio_bytes[8:12] != b"WAVE":
        raise malformed("仅支持 RIFF/WAVE 音频")
    riff_end = struct.unpack_from("<I", audio_bytes, 4)[0] + 8
    if riff_end != len(audio_bytes):
        raise malformed("WAV 容器长度不匹配，文件可能截断或附加了数据")
    position = 12
    data_size = None
    format_seen = False
    while position < riff_end:
        if position + 8 > riff_end:
            raise malformed("WAV 数据块头截断")
        name = audio_bytes[position:position + 4]
        size = struct.unpack_from("<I", audio_bytes, position + 4)[0]
        start = position + 8
        next_position = start + size + (size % 2)
        if next_position > riff_end:
            raise malformed("WAV 数据块截断")
        if name == b"fmt ":
            if format_seen or size < 16:
                raise malformed("WAV 格式块重复或不完整")
            format_seen = True
            _, channels, rate, byte_rate, block_align, bits = struct.unpack_from("<HHIIHH", audio_bytes, start)
            if bits != 16 or channels not in (1, 2):
                raise malformed("仅支持 16-bit PCM、单声道或双声道 WAV")
            if rate <= 0 or block_align != channels * 2 or byte_rate != rate * block_align:
                raise malformed("WAV 采样率、帧宽或字节率不一致")
        elif name == b"data":
            if data_size is not None:
                raise malformed("WAV 包含多个音频数据块，暂不支持")
            data_size = size
        position = next_position
    if not format_seen or data_size is None:
        raise malformed("WAV 缺少格式块或音频数据块")
    return data_size


def measure_wav(audio_bytes: bytes) -> tuple[int, int, int]:
    """实测 WAV，返回 (duration_ms, sample_rate_hz, channels)。

    以实际读到的帧数为准：头声明与实际数据不符（截断）按坏音频拒绝。
    """
    if len(audio_bytes) > MAX_FILE_BYTES:
        raise InspectError(413, "TOO_LARGE", "音频文件超过 100 MB 上限", "audio")
    data_size = _wav_data_size(audio_bytes)
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wf:
            channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            rate = wf.getframerate()
            nframes = wf.getnframes()
            if sampwidth != 2:
                raise InspectError(415, "UNSUPPORTED_AUDIO", "仅支持 16-bit PCM WAV", "audio")
            if channels not in (1, 2):
                raise InspectError(415, "UNSUPPORTED_AUDIO", "仅支持单声道或双声道", "audio")
            if rate <= 0:
                raise InspectError(415, "UNSUPPORTED_AUDIO", "采样率非法", "audio")
            if nframes <= 0:
                raise InspectError(415, "UNSUPPORTED_AUDIO", "音频没有有效帧", "audio")
            if data_size % (channels * sampwidth):
                raise InspectError(415, "UNSUPPORTED_AUDIO", "WAV 数据截断：存在不完整采样帧", "audio")
            # 先按精确帧数判断上限；不能让 600 秒加一帧被毫秒取整掩盖。
            if nframes * 1000 > MAX_DURATION_MS * rate:
                raise InspectError(413, "TOO_LARGE", "音频时长超过 10 分钟上限", "audio")
            data = wf.readframes(nframes)
    except InspectError:
        raise
    except (wave.Error, EOFError, struct.error, ValueError, OverflowError) as exc:
        raise InspectError(415, "UNSUPPORTED_AUDIO", f"无法解析的 WAV（{exc}）", "audio") from exc
    expected_bytes = nframes * channels * sampwidth
    if len(data) != expected_bytes:
        raise InspectError(
            415, "UNSUPPORTED_AUDIO",
            f"WAV 数据截断：头声明 {nframes} 帧，实际读到 {len(data) // (channels * sampwidth)} 帧",
            "audio",
        )
    duration_ms = nframes * 1000 // rate
    return duration_ms, rate, channels


def validate_boundaries(phrases: list, duration_ms: int) -> None:
    """按契约检查每句时间界限（422 级），field 精确到出错句子的字段。"""
    for i, phrase in enumerate(phrases):
        prefix = f"phrases[{i}]"
        lead, start, end = phrase["lead_ms"], phrase["start_ms"], phrase["end_ms"]
        if lead < 0:
            raise _bad_boundary("前导时间不能为负", f"{prefix}.lead_ms")
        if start < lead:
            raise _bad_boundary("开始时间早于前导时间", f"{prefix}.start_ms")
        if end <= start:
            raise _bad_boundary("结束时间必须晚于开始时间", f"{prefix}.end_ms")
        if end > duration_ms:
            raise _bad_boundary("结束时间超出音频", f"{prefix}.end_ms")
        entry = phrase["entry_ms"]
        if entry is not None and not (lead <= entry < end):
            raise _bad_boundary("入点必须位于 [lead_ms, end_ms) 内", f"{prefix}.entry_ms")


def inspect(audio_bytes: bytes, metadata) -> dict:
    """完整检查流程，成功返回契约 200 形状。失败抛 InspectError。"""
    request_id, revision_id, phrases = validate_metadata(metadata)
    duration_ms, rate, channels = measure_wav(audio_bytes)
    validate_boundaries(phrases, duration_ms)
    return {
        "request_id": request_id,
        "revision_id": revision_id,
        "processor_version": PROCESSOR_VERSION,
        "duration_ms": duration_ms,
        "sample_rate_hz": rate,
        "channels": channels,
        "phrase_count": len(phrases),
    }
