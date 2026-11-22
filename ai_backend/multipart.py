"""Strict two-field multipart adapter; preserve uploaded WAV bytes verbatim."""
from __future__ import annotations
from email import policy
from email.parser import BytesParser
import re
import uuid
from .inspection import InspectError

_BOUNDARY = re.compile(rb"[0-9A-Za-z'()+_,./:=? -]{1,70}")


def parse_multipart(body: bytes, boundary: bytes) -> dict[str, bytes]:
    def invalid(message):
        return InspectError(400, "INVALID_INPUT", message, "multipart")
    if not _BOUNDARY.fullmatch(boundary) or boundary.endswith(b" "):
        raise invalid("multipart boundary 无效或过长")
    if not body.startswith(b"--" + boundary + b"\r\n"):
        raise invalid("multipart 起始边界不完整")
    if not (body.endswith(b"\r\n--" + boundary + b"--\r\n")
            or body.endswith(b"\r\n--" + boundary + b"--")):
        raise invalid("multipart 结束边界不完整")
    header = b'Content-Type: multipart/form-data; boundary="' + boundary + b'"\r\n\r\n'
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(header + body)
        if not message.is_multipart() or message.defects:
            raise invalid("multipart 结构无效")
        parts = {}
        for part in message.get_payload():
            if part.is_multipart() or part.defects:
                raise invalid("multipart 不支持嵌套或损坏字段")
            if len(part.get_all("Content-Disposition", [])) != 1:
                raise invalid("multipart 需要唯一的 Content-Disposition")
            if part.get_content_disposition() != "form-data":
                raise invalid("multipart 字段必须使用 form-data")
            dispositions = part.get_params(header="content-disposition", failobj=[])
            if sum(key.lower() == "name" for key, _ in dispositions) != 1:
                raise invalid("multipart 字段需要唯一名称")
            name = part.get_param("name", header="content-disposition")
            if name not in ("audio", "metadata") or name in parts:
                raise invalid("multipart 只接受各一个 audio 和 metadata")
            if part.get("Content-Transfer-Encoding", "binary").lower() not in ("binary", "8bit", "7bit"):
                raise invalid("multipart 不接受编码转换，请直接上传原始字节")
            value = part.get_payload(decode=True)
            if not isinstance(value, bytes) or part.defects:
                raise invalid("multipart 字段内容不完整")
            parts[name] = value
    except InspectError:
        raise
    except (ValueError, TypeError, RecursionError) as error:
        raise invalid("multipart 解析失败") from error
    for name in ("audio", "metadata"):
        if name not in parts:
            raise InspectError(400, "INVALID_INPUT", f"缺少 {name} 字段", name)
    return parts


def build_multipart(fields: dict[str, bytes], filenames: dict[str, str] | None = None) -> tuple[bytes, str]:
    filenames = filenames or {}
    boundary = "----ai-inspect-" + uuid.uuid4().hex
    while any(boundary.encode("ascii") in value for value in fields.values()):
        boundary = "----ai-inspect-" + uuid.uuid4().hex
    chunks = []
    for name, data in fields.items():
        disposition = f'form-data; name="{name}"'
        if name in filenames:
            disposition += f'; filename="{filenames[name]}"'
        chunks.append(f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode("utf-8")
                      + data + b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), boundary
