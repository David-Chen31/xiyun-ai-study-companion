"""Generate review-only lyric candidates with a local Whisper checkpoint."""

import argparse
import json
import math
import re
import sys
import wave
from pathlib import Path


MAX_WINDOW_MS = 28000
MIN_WINDOW_MS = 500
MAX_HINT_LENGTH = 120
SAMPLE_RATE = 16000
TIMESTAMP = re.compile(r"<\|(\d{1,2}(?:\.\d+)?)\|>")


def timestamped_candidates(decoded, start_ms, end_ms):
    marks = list(TIMESTAMP.finditer(decoded))
    result = []
    for left, right in zip(marks, marks[1:]):
        lyric = decoded[left.end():right.start()].strip()
        start = start_ms + round(float(left[1]) * 1000)
        end = min(end_ms, start_ms + round(float(right[1]) * 1000))
        if not lyric or "<|" in lyric or not start_ms <= start < end or end - start < 100:
            continue
        if result and start < result[-1]["end_ms"]:
            continue
        result.append({"text": lyric[:200], "start_ms": start, "end_ms": end})
    return result[:40]


def read_audio(path, start_ms, end_ms, np, resample_poly):
    with wave.open(str(path), "rb") as wav:
        if wav.getsampwidth() != 2 or wav.getnchannels() not in (1, 2):
            raise ValueError("仅支持 PCM16 WAV")
        rate, channels = wav.getframerate(), wav.getnchannels()
        first, last = start_ms * rate // 1000, end_ms * rate // 1000
        if not 0 <= first < last <= wav.getnframes():
            raise ValueError("转写范围超出音轨")
        wav.setpos(first)
        pcm = np.frombuffer(wav.readframes(last - first), dtype="<i2")
    if pcm.size != (last - first) * channels:
        raise ValueError("转写音频已截断")
    values = pcm.astype(np.float32).reshape((-1, channels)).mean(axis=1) / 32768
    if rate != SAMPLE_RATE:
        divisor = math.gcd(rate, SAMPLE_RATE)
        values = resample_poly(values, SAMPLE_RATE // divisor, rate // divisor).astype(np.float32)
    return values


def transcribe(model_dir, audio, start_ms, end_ms, hint=""):
    if not 0 <= start_ms < end_ms or not MIN_WINDOW_MS <= end_ms - start_ms <= MAX_WINDOW_MS:
        raise ValueError("所选时间范围须为 0.5–28 秒")
    if not (model_dir / "model.safetensors").is_file():
        raise ValueError("找不到本机 Whisper safetensors 模型")
    if len(hint) > MAX_HINT_LENGTH or "<|" in hint:
        raise ValueError("参考唱词超长或包含模型控制符")

    import numpy as np
    import torch
    from scipy.signal import resample_poly
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    from transformers.utils.logging import disable_progress_bar

    signal = read_audio(audio, start_ms, end_ms, np, resample_poly)
    if float(np.max(np.abs(signal))) < .012 or float(np.sqrt(np.mean(signal ** 2))) < .003:
        return []
    torch.set_num_threads(4)
    disable_progress_bar()
    processor = WhisperProcessor.from_pretrained(model_dir, local_files_only=True)
    model = WhisperForConditionalGeneration.from_pretrained(model_dir, local_files_only=True, use_safetensors=True)
    model.eval()
    inputs = processor(signal, sampling_rate=SAMPLE_RATE, return_attention_mask=True, return_tensors="pt")
    parameters = {}
    if hint.strip():
        parameters["prompt_ids"] = processor.get_prompt_ids(hint.strip(), return_tensors="pt")[:120]
    with torch.inference_mode():
        output = model.generate(**inputs, language="zh", task="transcribe", return_timestamps=True,
                                do_sample=False, max_new_tokens=192, **parameters)
    tokens = output[0].reshape(-1).tolist()
    decoded = processor.tokenizer.decode(tokens, skip_special_tokens=True, decode_with_timestamps=True)
    return timestamped_candidates(decoded, start_ms, end_ms)


def main(argv=None):
    command = argparse.ArgumentParser(description="本机唱词候选，处理指定的 PCM16 WAV 窗口")
    command.add_argument("--model-dir", type=Path, required=True)
    command.add_argument("--audio", type=Path, required=True)
    command.add_argument("--start-ms", type=int, required=True)
    command.add_argument("--end-ms", type=int, required=True)
    command.add_argument("--hint-file", type=Path)
    args = command.parse_args(argv)
    try:
        hint = args.hint_file.read_text(encoding="utf-8").strip() if args.hint_file else ""
        candidates = transcribe(args.model_dir, args.audio, args.start_ms, args.end_ms, hint)
    except ImportError as error:
        print(f"本机转写依赖不可用：{error}", file=sys.stderr)
        return 2
    except (ValueError, OSError, RuntimeError, wave.Error) as error:
        print(f"本机转写未完成：{error}", file=sys.stderr)
        return 1
    print(json.dumps({"candidates": candidates}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
