const MAX_BYTES = 32 * 1024 * 1024;
const MAX_DURATION_MS = 600000;

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
    if (Number.isFinite(duration) && duration * 1000 > MAX_DURATION_MS) {
      throw new Error('音频超过 10 分钟。请先截成较短的片段。');
    }
  } finally {
    audio.onloadedmetadata = audio.onerror = null;
    audio.removeAttribute('src');
    audio.load();
    URL.revokeObjectURL(url);
  }
}

export async function decodeTrack(blob, fromMicrophone = false) {
  if (!blob.size) throw new Error('这个文件没有音频数据。');
  if (blob.size > MAX_BYTES) throw new Error('音频超过 32 MB。请选择较小的文件。');
  if (!fromMicrophone) await preflight(blob);
  const Context = globalThis.OfflineAudioContext || globalThis.webkitOfflineAudioContext;
  if (!Context) throw new Error('当前浏览器不支持本机音频解码。');
  const bytes = await blob.arrayBuffer();
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const fingerprint = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('');
  let buffer;
  try { buffer = await new Context(1, 1, 24000).decodeAudioData(bytes); }
  catch { throw new Error('无法读取这段音频。请试试有效的 WAV、MP3 或设备支持的音频格式。'); }
  const duration = Math.floor(buffer.duration * 1000);
  if (duration > MAX_DURATION_MS) throw new Error('音频超过 10 分钟。请先截成较短的片段。');
  if (duration < 100) throw new Error('音频不足 0.1 秒。');
  return {buffer, duration, fingerprint};
}
