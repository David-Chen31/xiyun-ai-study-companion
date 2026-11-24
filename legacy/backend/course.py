"""导入清单、许可和公共快照的数据边界。"""

import json
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlencode


PURPOSES = {"onsite_playback", "technical_processing"}
PHRASE_FIELDS = ("id", "text", "lead_ms", "start_ms", "entry_ms", "end_ms")


class CourseError(Exception):
    def __init__(self, status, code, message, field=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.field = field

    def body(self):
        body = {"code": self.code, "message": str(self)}
        if self.field:
            body["field"] = self.field
        return body


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def invalid(field, message):
    return CourseError(400, "INVALID_INPUT", message, field)


def read_json(path):
    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"重复字段 {key}")
            result[key] = value
        return result

    try:
        return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_object)
    except (ValueError, RecursionError) as error:
        raise invalid(None, f"无法读取 JSON：{path.name}（{error}）") from error


def require_text(data, key, prefix=""):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise invalid(prefix + key, "需要非空字符串")
    try:
        value.encode("utf-8")
    except UnicodeError as error:
        raise invalid(prefix + key, "包含非法 Unicode") from error
    return value


def utc_expiry(data):
    value = require_text(data, "expires_at")
    try:
        expiry = datetime.fromisoformat(value)
        if expiry.utcoffset() != timezone.utc.utcoffset(expiry) or "T" not in value:
            raise ValueError()
    except ValueError as error:
        raise invalid("expires_at", "请使用带 UTC 时区的 ISO 8601 时间，例如 2027-01-01T00:00:00Z") from error
    return expiry.isoformat(timespec="microseconds")


def normalize_grant(data):
    if not isinstance(data, dict):
        raise invalid(None, "许可须为 JSON 对象")
    digest = require_text(data, "audio_sha256").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise invalid("audio_sha256", "需要音频的 SHA-256 十六进制摘要")
    purposes = data.get("purposes")
    if (not isinstance(purposes, list)
            or any(not isinstance(purpose, str) for purpose in purposes)
            or not PURPOSES.issubset(purposes)):
        raise invalid("purposes", "许可须同时覆盖现场播放和技术处理")
    return {
        "grant_ref": require_text(data, "grant_ref"),
        "audio_sha256": digest,
        "credit": require_text(data, "credit"),
        "expires_at": utc_expiry(data),
        "purposes": sorted(set(purposes)),
    }


def normalize_course(data):
    if not isinstance(data, dict):
        raise invalid(None, "作业清单须为 JSON 对象")
    course = {key: require_text(data, key) for key in (
        "course_id", "revision_id", "media_id", "title", "credit",
        "author_id", "reviewer_id", "grant_ref",
    )}
    for key in ("course_id", "media_id"):
        value = course[key]
        if value in (".", "..") or re.search(r"[/\\\x00-\x1f\x7f]", value):
            raise invalid(key, "标识不能含路径分隔符、控制字符或点路径")
    if course["author_id"].strip().casefold() == course["reviewer_id"].strip().casefold():
        raise CourseError(403, "SELF_REVIEW", "作业需要独立复核人", "reviewer_id")
    course["expires_at"] = utc_expiry(data)
    phrases = data.get("phrases")
    if not isinstance(phrases, list) or not phrases:
        raise invalid("phrases", "作业至少需要一句")
    course["phrases"] = []
    for index, phrase in enumerate(phrases):
        prefix = f"phrases[{index}]."
        if not isinstance(phrase, dict):
            raise invalid(f"phrases[{index}]", "乐句须为 JSON 对象")
        for key in ("id", "text"):
            require_text(phrase, key, prefix)
        for key in PHRASE_FIELDS[2:]:
            if key not in phrase or (type(phrase[key]) is not int
                                     and not (key == "entry_ms" and phrase[key] is None)):
                raise invalid(prefix + key, "时间须为整数毫秒，入点可为空")
        course["phrases"].append({key: phrase[key] for key in PHRASE_FIELDS})
    return course


def check_phrase_windows(phrases, duration):
    """拒绝与 C 返回时长矛盾的成功结果。"""
    ids = set()
    for phrase in phrases:
        lead, start, entry, end = (phrase[key] for key in PHRASE_FIELDS[2:])
        if (phrase["id"] in ids or not 0 <= lead <= start < end <= duration
                or (entry is not None and not lead <= entry < end)):
            raise ValueError("句表与检查结果不一致")
        ids.add(phrase["id"])


def practice_path(course_id):
    return "/?" + urlencode({"course": course_id})


def public_course(course, duration):
    result = {key: course[key] for key in (
        "course_id", "revision_id", "media_id", "title", "credit", "phrases",
    )}
    media_id = quote(course["media_id"], safe="")
    query = urlencode({"course_id": course["course_id"]})
    result.update(duration_ms=duration, stream_url=f"/v1/media/{media_id}/stream?{query}")
    return result
