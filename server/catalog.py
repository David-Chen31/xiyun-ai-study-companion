"""Loopback catalog and prepared media for the karaoke client."""

import json
import re
from pathlib import Path
from threading import BoundedSemaphore


ROOT = Path(__file__).resolve().parent.parent
LIBRARY = ROOT / "library"
SONGS = json.loads((LIBRARY / "catalog.json").read_text(encoding="utf-8"))
MEDIA_ROUTE = re.compile(r"/media/karaoke/([a-z0-9-]+)/(reference|backing)\.mp3")
DETAIL_ROUTE = re.compile(r"/api/karaoke/songs/([a-z0-9-]+)")
PAGE_ROUTE = re.compile(r"/sing/[a-z0-9-]+")
MAX_PCM = 24_000 * 2 * 600
PITCH_SLOT = BoundedSemaphore(1)
COACH_SLOT = BoundedSemaphore(1)


def profile_path(song_id):
    return ROOT / ".xiyun" / "karaoke" / f"{song_id}.json"


def audio_path(name):
    root = (LIBRARY / "audio").resolve()
    path = (LIBRARY / name).resolve()
    return path if root in path.parents and path.suffix.lower() == ".mp3" and path.is_file() else None


def catalog_entry(song_id, song):
    return {
        "id": song_id,
        **{key: value for key, value in song.items() if key not in ("source", "backing")},
        "available": all(audio_path(name) is not None for name in (song["source"], song.get("backing", song["source"]))),
        "prepared": profile_path(song_id).is_file(),
    }


def dispatch(handler, path):
    if path == "/api/karaoke/songs":
        handler._reply(200, encode([catalog_entry(key, song) for key, song in SONGS.items()]),
                       "application/json; charset=utf-8")
        return True
    if detail := DETAIL_ROUTE.fullmatch(path):
        song_id = detail[1]
        song = SONGS.get(song_id)
        if not song or not catalog_entry(song_id, song)["available"]:
            error(handler, 404, "本机没有找到这首曲目的音频。")
        elif not profile_path(song_id).is_file():
            error(handler, 503, "这首曲目的歌词与评测数据尚未准备好。")
        else:
            try:
                profile = json.loads(profile_path(song_id).read_text(encoding="utf-8"))
                if profile["id"] != song_id or not profile["lines"]:
                    raise ValueError("profile")
                detail_data = {
                    **catalog_entry(song_id, song),
                    **profile,
                    "reference_url": f"/media/karaoke/{song_id}/reference.mp3",
                    "backing_url": f"/media/karaoke/{song_id}/backing.mp3",
                    "backing_is_mix": song.get("backing", song["source"]) == song["source"],
                }
                handler._reply(200, encode(detail_data), "application/json; charset=utf-8")
            except (OSError, KeyError, TypeError, ValueError):
                error(handler, 503, "曲目的本机评测数据无法读取。")
        return True
    if media := MEDIA_ROUTE.fullmatch(path):
        song = SONGS.get(media[1])
        key = "backing" if media[2] == "backing" else "source"
        file = audio_path(song.get(key, song["source"])) if song else None
        if file is None:
            error(handler, 404, "找不到本机音频。")
        else:
            stream_mp3(handler, file)
        return True
    return False


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def error(handler, status, message):
    handler._reply(status, encode({"message": message}), "application/json; charset=utf-8")


def dispatch_post(handler, path):
    if path not in ("/api/karaoke/pitch", "/api/karaoke/coach"):
        return False
    origin = handler.headers.get("Origin")
    if origin and origin != "http://" + handler.headers.get("Host", ""):
        error(handler, 403, "请从本机练唱台发起分析。")
        return True
    pitch = path.endswith("/pitch")
    try:
        size = int(handler.headers.get("Content-Length", "0"))
        if not 0 < size <= (MAX_PCM if pitch else 4096) or pitch and size % 2:
            raise ValueError
        body = handler.rfile.read(size)
        if len(body) != size:
            raise ValueError
    except ValueError:
        error(handler, 400, "分析输入长度无效。")
        return True
    slot = PITCH_SLOT if pitch else COACH_SLOT
    if not slot.acquire(blocking=False):
        error(handler, 503, "本机模型正在分析另一段。")
        return True
    try:
        if pitch:
            from .pitch import ENGINE, MODEL_PATH, extract

            if not MODEL_PATH.is_file():
                error(handler, 503, "本机音高模型尚未安装。")
                return True
            data = {"engine": ENGINE, "contour": extract(body)}
        else:
            data = coach_reply(json.loads(body))
        handler._reply(200, encode(data), "application/json; charset=utf-8")
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        pass
    except (ImportError, OSError, RuntimeError, AttributeError, KeyError, TypeError, ValueError):
        error(handler, 503, "本机模型暂时无法生成结果。")
    finally:
        slot.release()
    return True


def coach_reply(body):
    from .coach import coach

    song_id = body["song_id"]
    if song_id not in SONGS:
        raise ValueError("song")
    profile = json.loads(profile_path(song_id).read_text(encoding="utf-8"))
    line = next((item for item in profile["lines"] if item["id"] == body["line_id"]), None)
    if line is None:
        raise ValueError("line")
    facts = body["facts"]
    clean = {}
    for key in ("pitch_score", "coverage", "matched_percent"):
        value = facts[key]
        if type(value) is not int or not 0 <= value <= 100:
            raise ValueError(key)
        clean[key] = value
    if clean["coverage"] < 20:
        raise ValueError("coverage")
    for key in ("delta_cents", "spread_cents", "duration_ms"):
        value = facts.get(key)
        if value is not None:
            if type(value) is not int or (key == "duration_ms" and not 0 <= value <= 15_000) or (
                key != "duration_ms" and not -700 <= value <= 700
            ):
                raise ValueError(key)
            clean[key] = value
    position = facts.get("position")
    if position in ("句首", "句中", "句尾"):
        clean["position"] = position
    note = facts.get("target_note")
    if isinstance(note, str) and re.fullmatch(r"[A-G]♯?[1-6]", note) and "delta_cents" in clean:
        clean["target_note"] = note
    if facts.get("register") in (-12, 12):
        clean["register"] = facts["register"]
    stamp = facts.get("reference_ms")
    if type(stamp) is int and "delta_cents" in clean:
        word = next((item for item in line.get("words", [])
                     if item["text"] == facts.get("aligned_word") and item["start_ms"] <= stamp < item["end_ms"]), None)
        if word:
            clean["aligned_word"] = word["text"]
            if word.get("pinyin"):
                clean["pinyin"] = word["pinyin"]
    return {"engine": "qwen3-local", **coach(line["text"][:160], clean)}


def stream_mp3(handler, file):
    size = file.stat().st_size
    first, last, status = 0, size - 1, 200
    requested = handler.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
        if match and any(match.groups()):
            begin, end = match.groups()
            if len(begin) <= 16 and len(end) <= 16:
                if begin and int(begin) < size and (not end or int(end) >= int(begin)):
                    first, last, status = int(begin), min(size - 1, int(end)) if end else size - 1, 206
                elif not begin and int(end) > 0:
                    first, status = max(0, size - int(end)), 206
        if status != 206:
            handler._reply(416, headers={"Content-Range": f"bytes */{size}", "Accept-Ranges": "bytes"})
            return
    handler.send_response(status)
    handler.send_header("Content-Type", "audio/mpeg")
    handler.send_header("Content-Length", str(last - first + 1))
    handler.send_header("Accept-Ranges", "bytes")
    handler.send_header("Cache-Control", "private, no-store")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {first}-{last}/{size}")
    handler.end_headers()
    if handler.command == "HEAD":
        return
    try:
        with file.open("rb") as content:
            content.seek(first)
            remaining = last - first + 1
            while remaining:
                chunk = content.read(min(65536, remaining))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)
    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        return
