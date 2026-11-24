"""受控的本机导入转码，输出 PCM16 WAV 和原媒体时钟信息。"""

import json
import io
import shutil
import subprocess
import sys
import tempfile
import wave
from array import array
from fractions import Fraction
from pathlib import Path

from .inspection import MAX_FILE_BYTES

class MediaPreparationError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def media_type(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[4:8] == b"ftyp":
        return "mp4"
    if data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0):
        return "mp3"
    raise MediaPreparationError("UNSUPPORTED_MEDIA", "只支持 PCM WAV、MP3 和带音轨的 MP4")


def run_media(command, timeout):
    flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
    try:
        return subprocess.run(command, check=True, capture_output=True, timeout=timeout, creationflags=flags).stdout
    except subprocess.TimeoutExpired as error:
        raise MediaPreparationError("CONVERSION_TIMEOUT", "转码超时，请缩短源素材后重试") from error
    except (OSError, subprocess.CalledProcessError) as error:
        raise MediaPreparationError("INVALID_MEDIA", "无法解码音轨，请检查输入格式及文件完整性") from error


def mp4_clocks(ffprobe, path):
    report = run_media([
        ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", "mov", "-enable_drefs", "0",
        "-show_entries", "stream=codec_type,start_time:format=duration", "-of", "json", str(path),
    ], 10)
    try:
        data = json.loads(report)
        starts = {}
        for stream in data["streams"]:
            kind = stream["codec_type"]
            if kind in ("audio", "video") and kind not in starts:
                starts[kind] = round(Fraction(stream["start_time"]) * 1000)
        duration = Fraction(data["format"]["duration"])
        return starts["audio"], starts["video"], duration
    except (KeyError, ValueError, TypeError, ZeroDivisionError) as error:
        raise MediaPreparationError("INVALID_MEDIA", "MP4 需同时含可测量起点的画面与音轨") from error


def make_video_preview(ffmpeg, ffprobe, source, path, input_options, source_offset):
    run_media([
        ffmpeg, "-v", "error", "-nostdin", "-copyts", "-start_at_zero", *input_options, "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0", "-sn", "-dn", "-map_metadata", "-1", "-map_chapters", "-1",
        "-t", "601", "-vf", "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2",
        "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-threads:v", "2",
        "-crf", "28", "-maxrate:v", "900k", "-bufsize:v", "1800k",
        "-c:a", "aac", "-ac", "1", "-ar", "48000", "-b:a", "80k", "-movflags", "+faststart", str(path),
    ], 90)
    audio_start, video_start, duration = mp4_clocks(ffprobe, path)
    if duration <= 0 or duration > 600 or path.stat().st_size > MAX_FILE_BYTES:
        raise MediaPreparationError("PREVIEW_TOO_LARGE", "视频预览超过时长或大小上限")
    if abs((audio_start - video_start) - source_offset) > 60:
        raise MediaPreparationError("PREVIEW_CLOCK_MISMATCH", "预览转码后的音画起点偏移过大")
    # The encoded track timestamp includes AAC priming; editor time follows the source audio samples.
    return path.read_bytes(), {"audio_start_ms": video_start + source_offset, "video_start_ms": video_start}


def prepare_media(data, source_type, *, video_preview=False):
    if source_type == "wav":
        return data, {"source_type": "wav", "audio_offset_ms": 0}, None
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or (source_type == "mp4" and not ffprobe):
        raise MediaPreparationError("CONVERTER_UNAVAILABLE", "此机器需要 FFmpeg 才能转码，也可上传 PCM WAV")
    demuxer = "mov" if source_type == "mp4" else "mp3"
    with tempfile.TemporaryDirectory(prefix="xiyun-media-") as directory:
        source = Path(directory) / "source"
        output = Path(directory) / "audio.wav"
        source.write_bytes(data)
        # The demuxer and protocol are fixed so the file cannot choose remote media inputs.
        input_options = ["-protocol_whitelist", "file", "-f", demuxer]
        if source_type == "mp4":
            input_options.extend(("-enable_drefs", "0"))
        offset = 0
        preview = None
        if source_type == "mp4":
            audio_start, video_start, video_duration = mp4_clocks(ffprobe, source)
            offset = audio_start - video_start
            if video_preview and not 0 < video_duration <= 600:
                raise MediaPreparationError("PREVIEW_TOO_LARGE", "视频素材须在 10 分钟以内")
        run_media([
            ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", *input_options, "-i", str(source),
            "-map", "0:a:0", "-vn", "-sn", "-dn", "-t", "601", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", str(output),
        ], 30)
        clocks = {"source_type": source_type, "audio_offset_ms": offset}
        if source_type == "mp4" and video_preview:
            preview, clocks["preview"] = make_video_preview(
                ffmpeg, ffprobe, source, Path(directory) / "preview.mp4", input_options, offset,
            )
        return output.read_bytes(), clocks, preview


def waveform(wav, divisions=160):
    """定长峰值包络，供教师定位。标注仍使用原媒体毫秒。"""
    with wave.open(io.BytesIO(wav), "rb") as source:
        columns = min(divisions, source.getnframes())
        boundaries = [i * source.getnframes() // columns for i in range(columns + 1)]
        result = []
        for start, end in zip(boundaries, boundaries[1:]):
            samples = array("h", source.readframes(end - start))
            if sys.byteorder != "little":
                samples.byteswap()
            peak = max(abs(min(samples)), abs(max(samples))) if samples else 0
            result.append(round(peak / 32768, 3))
        return result


def activity_windows(envelope, duration_ms):
    """以音量找可供人工试听的连续区间，不推断人声、角色或唱词。"""
    if not envelope or max(envelope) < 0.015:
        return []
    threshold = max(0.01, max(envelope) * 0.12)
    activity = []
    start = None
    columns = len(envelope)
    for index, level in enumerate([*envelope, 0]):
        if level >= threshold:
            if start is None:
                start = index
        elif start is not None:
            left = start * duration_ms // columns
            right = index * duration_ms // columns
            if right - left >= 120:
                activity.append({"start_ms": left, "end_ms": right})
            start = None
    return activity[:40]
