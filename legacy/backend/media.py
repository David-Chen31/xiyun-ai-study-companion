"""Read a PCM slice without exposing the storage path or the rest of the audio."""

import io
import wave

from .course import CourseError


def slice_wav(db, digest, start_ms, end_ms):
    media = db.execute("SELECT rowid FROM media WHERE digest=?", (digest,)).fetchone()
    if media is None:
        raise CourseError(404, "MEDIA_UNKNOWN", "找不到音频")
    with db.blobopen("media", "wav", media["rowid"], readonly=True) as blob, wave.open(blob, "rb") as source:
        rate = source.getframerate()
        start = start_ms * rate // 1000
        end = end_ms * rate // 1000
        if not 0 <= start < end <= source.getnframes():
            raise CourseError(422, "INVALID_BOUNDARY", "时间窗口超出音轨")
        source.setpos(start)
        audio = source.readframes(end - start)
        output = io.BytesIO()
        with wave.open(output, "wb") as segment:
            segment.setnchannels(source.getnchannels())
            segment.setsampwidth(source.getsampwidth())
            segment.setframerate(rate)
            segment.writeframes(audio)
    return output.getvalue()
