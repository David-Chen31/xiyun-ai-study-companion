"""极简 multipart/form-data 解析与构造（标准库实现，二进制安全）。

只覆盖本契约需要的场景：两个字段 audio（文件）与 metadata（文本）。
"""

from __future__ import annotations

import re
import uuid

_NAME_RE = re.compile(rb'name="([^"]*)"')


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, bytes]:
    """按边界切分 multipart 体，返回 {字段名: 原始字节}。

    直接在字节层切分，不做任何编码转换，WAV 二进制内容原样保留。
    """
    delimiter = b"--" + boundary
    parts: dict[str, bytes] = {}
    segments = body.split(delimiter)
    for segment in segments[1:]:
        if segment.startswith(b"--"):
            break  # 结束边界 --boundary--
        segment = segment.removeprefix(b"\r\n")
        head, sep, content = segment.partition(b"\r\n\r\n")
        if not sep:
            continue
        content = content.removesuffix(b"\r\n")
        match = _NAME_RE.search(head)
        if match is None:
            continue
        name = match.group(1).decode("utf-8", errors="replace")
        parts[name] = content
    return parts


def build_multipart(fields: dict[str, bytes], filenames: dict[str, str] | None = None) -> tuple[bytes, str]:
    """构造 multipart 体，返回 (body, boundary)。供重放脚本与测试使用。"""
    filenames = filenames or {}
    boundary = "----ai-inspect-" + uuid.uuid4().hex
    chunks: list[bytes] = []
    for name, data in fields.items():
        disposition = f'form-data; name="{name}"'
        if name in filenames:
            disposition += f'; filename="{filenames[name]}"'
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode("utf-8")
            + data
            + b"\r\n"
        )
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary