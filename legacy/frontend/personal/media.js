import { createDemoWav, measureWaveform, PhrasePlayer, VoiceRecorder } from '../src/audio.js';

export const MAX_BYTES = 32 * 1024 * 1024;
export const MAX_DURATION = 600000;
export const CAPTURE_LIMIT = 60000;

async function preflight(blob) {
  const audio = new Audio();
  const url = URL.createObjectURL(blob);
  try {
    const duration = await new Promise(resolve => {
      const finish = value => { clearTimeout(timer); resolve(value); };
      const timer = setTimeout(() => finish(null), 2500);
      audio.preload = 'metadata';
      audio.onloadedmetadata = () => finish(audio.duration);
      audio.onerror = () => finish(null);
      audio.src = url;
    });
    if (Number.isFinite(duration) && duration * 1000 > MAX_DURATION) {
      throw new Error('音频超过 10 分钟。请先截成较短的片段。');
    }
  } finally {
    audio.onloadedmetadata = audio.onerror = null;
    audio.removeAttribute('src');
    audio.load();
    URL.revokeObjectURL(url);
  }
}

export async function decodeTrack(blob, name, source = 'file') {
  if (!blob.size) throw new Error('这个文件没有音频数据。');
  if (blob.size > MAX_BYTES) throw new Error('音频超过 32 MB。请选择较小的文件。');
  if (source === 'file') await preflight(blob);
  const Context = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  if (!Context) throw new Error('当前浏览器不支持本机音频解码。');
  const bytes = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const fingerprint = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  let buffer;
  try { buffer = await new Context(1, 1, 24000).decodeAudioData(bytes); }
  catch { throw new Error('无法读取这段音频。请试试有效的 WAV、MP3 或设备支持的音频格式。'); }
  const duration = Math.floor(buffer.duration * 1000);
  if (duration > MAX_DURATION) throw new Error('音频超过 10 分钟。请先截成较短的片段。');
  if (duration < 100) throw new Error('音频不足 0.1 秒。');
  return {id:crypto.randomUUID(), blob, buffer, name, source, duration, fingerprint,
    peaks:measureWaveform(buffer, 0, buffer.duration, 128), start:0, end:duration};
}

export async function openExample() {
  const blob = new Blob([createDemoWav()], {type:'audio/wav'});
  const track = await decodeTrack(blob, '三句合成测试音', 'example');
  track.end = 3900;
  return track;
}

export function analysisClip(track) {
  const rate = track.buffer.sampleRate;
  const channel = track.buffer.getChannelData(0);
  const first = Math.round(track.start * rate / 1000);
  const last = Math.min(channel.length, Math.round(track.end * rate / 1000));
  return {samples:channel.slice(first, last), sampleRate:rate, start:track.start};
}

const bufferPlayer = new PhrasePlayer({provider:{getBuffer: track => track.buffer}});
const slowAudio = new Audio();
let slowUrl, frameId;
let generation = 0;
export const canPreservePitch = 'preservesPitch' in slowAudio;
const MAX_SLOW_SEGMENT_MS = 60000;

// A local PCM clip ends at the selection boundary without a polling-based seek or stop.
function playbackClip(track, {lead_ms, end_ms}) {
  const {buffer} = track;
  const rate = buffer.sampleRate;
  const start = Math.round(lead_ms * rate / 1000);
  const end = Math.min(buffer.length, Math.round(end_ms * rate / 1000));
  const channels = Array.from({length:buffer.numberOfChannels}, (_, index) => buffer.getChannelData(index));
  const frameSize = channels.length * 2;
  const size = (end - start) * frameSize;
  const bytes = new ArrayBuffer(44 + size);
  const view = new DataView(bytes);
  const label = (offset, value) => {
    for (let index = 0; index < value.length; index++) view.setUint8(offset + index, value.charCodeAt(index));
  };
  label(0, 'RIFF');
  view.setUint32(4, 36 + size, true);
  label(8, 'WAVE');
  label(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channels.length, true);
  view.setUint32(24, rate, true);
  view.setUint32(28, rate * frameSize, true);
  view.setUint16(32, frameSize, true);
  view.setUint16(34, 16, true);
  label(36, 'data');
  view.setUint32(40, size, true);
  let offset = 44;
  for (let sample = start; sample < end; sample++) {
    for (const channel of channels) {
      view.setInt16(offset, Math.round(Math.max(-1, Math.min(1, channel[sample])) * 32767), true);
      offset += 2;
    }
  }
  return new Blob([bytes], {type:'audio/wav'});
}

function stopSlowPlayback() {
  if (!slowUrl) return;
  cancelAnimationFrame(frameId);
  slowAudio.onended = slowAudio.onerror = null;
  slowAudio.pause();
  slowAudio.removeAttribute('src');
  slowAudio.load();
  URL.revokeObjectURL(slowUrl);
  slowUrl = null;
}

export const player = {
  stop() {
    generation++;
    stopSlowPlayback();
    bufferPlayer.stop();
  },
  close() {
    this.stop();
    return bufferPlayer.close();
  },
  async play(track, range, {rate, onProgress, onEnded, onFallback, onError}) {
    this.stop();
    const attempt = generation;
    if (rate !== 1 && canPreservePitch && range.end_ms - range.lead_ms <= MAX_SLOW_SEGMENT_MS) {
      const fallback = async () => {
        if (attempt !== generation || !slowUrl) return;
        stopSlowPlayback();
        onFallback();
        bufferPlayer.setRate(rate);
        try { await bufferPlayer.play(track, range, {onProgress, onEnded}); }
        catch (error) { if (attempt === generation) onError(error); }
      };
      slowUrl = URL.createObjectURL(playbackClip(track, range));
      slowAudio.src = slowUrl;
      slowAudio.preservesPitch = true;
      slowAudio.playbackRate = rate;
      slowAudio.onended = () => {
        if (attempt !== generation) return;
        stopSlowPlayback();
        onProgress({positionMs:range.end_ms});
        onEnded();
      };
      slowAudio.onerror = () => void fallback();
      try { await slowAudio.play(); }
      catch { await fallback(); return; }
      const tick = () => {
        if (attempt !== generation || !slowUrl) return;
        const positionMs = Math.min(range.end_ms, range.lead_ms + Math.round(slowAudio.currentTime * 1000));
        onProgress({positionMs});
        frameId = requestAnimationFrame(tick);
      };
      tick();
      return;
    }
    if (rate !== 1) onFallback();
    bufferPlayer.setRate(rate);
    await bufferPlayer.play(track, range, {onProgress, onEnded});
  }
};
export const recorder = new VoiceRecorder();
