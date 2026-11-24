"""Extract an aligned vocal reference using the UVR Voc FT ONNX model."""

import argparse
import subprocess
from pathlib import Path

import numpy as np
import onnxruntime as ort
from scipy.fft import irfft, rfft
from scipy.io import wavfile
from scipy.signal.windows import hann


RATE = 44_100
FFT_SIZE = 7_680
HOP = 1_024
FRAMES = 256
BINS = 3_072
TRIM = FFT_SIZE // 2
CHUNK = (FRAMES - 1) * HOP
OUTPUT = CHUNK - 2 * TRIM
WINDOW = hann(FFT_SIZE, sym=False).astype(np.float32)


def decode(path):
    pcm = subprocess.check_output([
        "ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "2",
        "-ar", str(RATE), "-f", "f32le", "pipe:1",
    ])
    return np.frombuffer(pcm, dtype="<f4").reshape(-1, 2).T


def separate(session, samples):
    padded = np.pad(samples, ((0, 0), (TRIM, TRIM + (-samples.shape[-1] % OUTPUT))))
    mono = np.empty(padded.shape[-1] - 2 * TRIM, dtype=np.float32)
    window_gain = np.zeros(CHUNK + FFT_SIZE, dtype=np.float32)
    for index in range(FRAMES):
        window_gain[index * HOP:index * HOP + FFT_SIZE] += WINDOW ** 2
    for start in range(0, mono.size, OUTPUT):
        segment = padded[:, start:start + CHUNK]
        reflected = np.pad(segment, ((0, 0), (TRIM, TRIM)), mode="reflect")
        frames = np.lib.stride_tricks.sliding_window_view(reflected, FFT_SIZE, axis=-1)[:, ::HOP]
        spectrum = rfft(frames * WINDOW).transpose(0, 2, 1)[:, :BINS]
        features = np.stack((spectrum.real, spectrum.imag), axis=1).reshape(1, 4, BINS, FRAMES)
        features[:, :, :3] = 0
        predicted = session.run(None, {"input": features})[0][0].reshape(2, 2, BINS, FRAMES)
        voice = np.mean(predicted[:, 0] + 1j * predicted[:, 1], axis=0).T
        reconstructed = irfft(voice, n=FFT_SIZE) * WINDOW
        output = np.zeros(CHUNK + FFT_SIZE, dtype=np.float32)
        for index, frame in enumerate(reconstructed):
            output[index * HOP:index * HOP + FFT_SIZE] += frame
        inner = slice(2 * TRIM, CHUNK)
        mono[start:start + OUTPUT] = output[inner] / window_gain[inner]
        if (start // OUTPUT + 1) % 10 == 0:
            print(f"已分离 {min(samples.shape[-1], start + OUTPUT) / RATE:.0f} 秒", flush=True)
    return mono[:samples.shape[-1]]


def main():
    parser = argparse.ArgumentParser(description="用 UVR-MDX-NET-Voc_FT 生成等长单声道人声参考")
    parser.add_argument("audio", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    options = ort.SessionOptions()
    options.intra_op_num_threads = 4
    session = ort.InferenceSession(str(args.model), sess_options=options, providers=["CPUExecutionProvider"])
    if session.get_inputs()[0].shape[1:] != [4, BINS, FRAMES]:
        parser.error("模型维度与 UVR-MDX-NET-Voc_FT 不符")
    vocals = separate(session, decode(args.audio))
    target = args.output.with_suffix(".tmp.wav")
    wavfile.write(target, RATE, np.round(np.clip(vocals, -1, 1) * 32767).astype(np.int16))
    target.replace(args.output)


if __name__ == "__main__":
    main()
