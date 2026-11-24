import { CourseError, playbackWindow, fetchResource } from './course.js';

// Original synthetic test signal: 12 seconds, three distinguishable pitches.
// No teacher recording or external media is bundled with the day-one prototype.
export function createDemoWav() {
  const rate = 16000;
  const frames = rate * 12;
  const buffer = new ArrayBuffer(44 + frames * 2);
  const view = new DataView(buffer);
  const label = (offset, value) => [...value].forEach((character, i) => view.setUint8(offset + i, character.charCodeAt(0)));
  label(0, 'RIFF'); view.setUint32(4, 36 + frames * 2, true); label(8, 'WAVE');
  label(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
  view.setUint32(24, rate, true); view.setUint32(28, rate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
  label(36, 'data'); view.setUint32(40, frames * 2, true);
  for (let i = 0; i < frames; i++) {
    const time = i / rate;
    const local = time % 1;
    const envelope = local < .65 ? Math.min(local / .03, 1, (.65 - local) / .06) : 0;
    const frequency = time < 4 ? 262 : time < 8.1 ? 330 : 392;
    view.setInt16(44 + i * 2, Math.round(Math.sin(2 * Math.PI * frequency * time) * envelope * 2400), true);
  }
  return buffer;
}

export class DemoAudioProvider {
  async getBytes() { return createDemoWav(); }
}

export function measureWaveform(buffer, offset = 0, duration = buffer.duration, divisions = 84) {
  const channel = buffer.getChannelData(0);
  const start = Math.round(offset * buffer.sampleRate);
  const length = Math.min(channel.length - start, Math.round(duration * buffer.sampleRate));
  const count = Math.min(divisions, length);
  return Array.from({length:count}, (_, index) => {
    const from = start + Math.floor(index * length / count);
    const to = start + Math.floor((index + 1) * length / count);
    const stride = Math.max(1, Math.floor((to - from) / 512));
    let peak = 0;
    for (let i = from; i < to; i += stride) peak = Math.max(peak, Math.abs(channel[i]));
    return peak;
  });
}

export class ApiAudioProvider {
  #fetcher;
  constructor(fetcher = globalThis.fetch) { this.#fetcher = fetcher.bind(globalThis); }

  async getBytes(course, signal, phrase) {
    let url = course.stream_url;
    if (course.phrase_stream && phrase) {
      url = new URL(url, globalThis.location.origin);
      url.searchParams.set('phrase_id', phrase.id);
    }
    const response = await fetchResource(this.#fetcher, url, signal);
    return response.arrayBuffer();
  }
}

function browserAudioContext() {
  const AudioContextClass = globalThis.AudioContext || globalThis.webkitAudioContext;
  if (!AudioContextClass) throw new CourseError('AUDIO', '当前浏览器暂不支持试听，请换用新版浏览器。');
  return new AudioContextClass();
}

export class PhrasePlayer {
  #provider;
  #contextFactory;
  #context = null;
  #source = null;
  #generation = 0;
  #frameId = null;
  #requestFrame;
  #cancelFrame;
  #timing = null;
  #rate = 1;

  constructor({ provider, contextFactory = browserAudioContext, requestFrame = globalThis.requestAnimationFrame?.bind(globalThis), cancelFrame = globalThis.cancelAnimationFrame?.bind(globalThis) }) {
    this.#provider = provider;
    this.#contextFactory = contextFactory;
    this.#requestFrame = requestFrame;
    this.#cancelFrame = cancelFrame;
  }

  setRate(rate) {
    if (this.#source) {
      this.#timing.elapsed = this.#elapsed();
      this.#timing.clock = this.#context.currentTime;
      this.#source.playbackRate.value = rate;
    }
    this.#rate = rate;
  }

  #elapsed() {
    const time = this.#timing;
    return Math.min(time.duration, time.elapsed + (this.#context.currentTime - time.clock) * this.#rate);
  }

  async unlock() {
    this.#context ??= this.#contextFactory();
    await this.#context.resume();
  }

  async play(course, phrase, { signal, onEnded = () => {}, onProgress = () => {}, onWaveform } = {}) {
    this.stop();
    const generation = this.#generation;
    const assertActive = () => {
      if (generation !== this.#generation || signal?.aborted) throw new DOMException('Playback cancelled', 'AbortError');
    };
    await this.unlock();
    assertActive();
    const context = this.#context;
    let buffer;
    if (this.#provider.getBuffer) buffer = await this.#provider.getBuffer(course, signal, phrase);
    else {
      const bytes = await this.#provider.getBytes(course, signal, phrase);
      assertActive();
      buffer = await context.decodeAudioData(bytes);
    }
    assertActive();
    const segment = playbackWindow(phrase);
    const offset = course.phrase_stream ? 0 : segment.offset;
    if (buffer.duration + .001 < offset + segment.duration) throw new CourseError('AUDIO', '示范音频与句子时间不一致，请联系老师检查。');
    if (onWaveform && typeof buffer.getChannelData === 'function') {
      const peaks = measureWaveform(buffer, offset, segment.duration);
      if (peaks.length) onWaveform(peaks);
    }
    const node = context.createBufferSource();
    node.buffer = buffer;
    node.playbackRate.value = this.#rate;
    node.connect(context.destination);
    this.#timing = { clock: context.currentTime, elapsed: 0, duration: segment.duration };
    const report = elapsed => onProgress({
      positionMs: Math.round(phrase.lead_ms + elapsed * 1000),
      ratio: elapsed / segment.duration
    });
    const tick = () => {
      if (this.#source !== node) return;
      this.#frameId = null;
      if (signal?.aborted) { this.stop(); return; }
      const elapsed = this.#elapsed();
      report(elapsed);
      if (elapsed < segment.duration && this.#requestFrame) this.#frameId = this.#requestFrame(tick);
    };
    node.onended = () => {
      if (this.#source !== node) return;
      this.#cancelProgress();
      this.#source = null;
      node.disconnect();
      report(segment.duration);
      onEnded();
    };
    this.#source = node;
    try { node.start(0, offset, segment.duration); tick(); }
    catch (error) { this.stop(); throw error; }
  }

  stop() {
    this.#generation++;
    this.#cancelProgress();
    if (!this.#source) return;
    const node = this.#source;
    this.#source = null;
    this.#timing = null;
    node.onended = null;
    node.stop();
    node.disconnect();
  }

  #cancelProgress() {
    if (this.#frameId !== null) this.#cancelFrame?.(this.#frameId);
    this.#frameId = null;
  }

  async close() {
    this.stop();
    const context = this.#context;
    this.#context = null;
    if (context && context.state !== 'closed') await context.close();
  }
}

// Captures and replays only local blobs. Nothing is uploaded or persisted.
export class VoiceRecorder {
  #getUserMedia;
  #recorderClass;
  #audioFactory;
  #urls;
  #now;
  #capture = null;
  #audio = null;
  #generation = 0;
  #clips = new Map();

  constructor({
    getUserMedia = constraints => globalThis.navigator.mediaDevices.getUserMedia(constraints),
    recorderClass = globalThis.MediaRecorder,
    audioFactory = () => new Audio(), urls = globalThis.URL,
    now = () => performance.now()
  } = {}) {
    this.#getUserMedia = getUserMedia; this.#recorderClass = recorderClass;
    this.#audioFactory = audioFactory; this.#urls = urls; this.#now = now;
  }

  clip(key) { return this.#clips.get(key); }

  async start(key, { onTick = () => {}, onComplete = () => {}, onError = () => {}, onInterrupted } = {}) {
    this.cancel();
    if (!this.#recorderClass) throw new CourseError('MIC', '当前浏览器不支持录音，请换用新版浏览器。仍可听示范。');
    const generation = this.#generation;
    const stream = await this.#getUserMedia({ audio: true, video: false });
    const release = () => stream.getTracks().forEach(track => track.stop());
    if (generation !== this.#generation) { release(); throw new DOMException('Recording cancelled', 'AbortError'); }
    let capture;
    try {
      const recorder = new this.#recorderClass(stream);
      let resolve;
      const done = new Promise(yes => { resolve = yes; });
      capture = { recorder, release, chunks: [], startedAt: this.#now(), stoppedAt: null, done, resolve };
      this.#capture = capture;
      recorder.ondataavailable = event => { if (event.data.size) capture.chunks.push(event.data); };
      recorder.onstop = () => {
        this.#stopCapture(capture);
        if (this.#capture !== capture) { resolve(null); return; }
        this.#capture = null;
        const blob = new Blob(capture.chunks, { type: recorder.mimeType || capture.chunks[0]?.type });
        if (!blob.size) { resolve(null); onError(new CourseError('MIC', '没有录到声音，请检查麦克风后重录。')); return; }
        const previous = this.clip(key);
        const clip = {
          blob, url: this.#urls.createObjectURL(blob), take: (previous?.take ?? 0) + 1,
          durationMs: Math.round((capture.stoppedAt ?? this.#now()) - capture.startedAt)
        };
        if (previous) this.#urls.revokeObjectURL(previous.url);
        this.#clips.set(key, clip); resolve(clip); onComplete(clip);
      };
      recorder.onerror = () => {
        if (this.#capture !== capture) return;
        this.cancel(); onError(new CourseError('MIC', '录音被设备中断，请检查麦克风后重录。'));
      };
      stream.getTracks().forEach(track => {
        track.onended = () => {
          if (this.#capture !== capture) return;
          if (!onInterrupted) { this.stop(); return; }
          this.cancel();
          onInterrupted();
        };
      });
      recorder.start();
      onTick(0);
      capture.timer = setInterval(() => { if (this.#capture === capture) onTick(this.#now() - capture.startedAt); }, 200);
    } catch (error) {
      if (capture) { this.#capture = null; clearInterval(capture.timer); capture.resolve(null); }
      release(); throw error;
    }
  }

  stop() {
    const capture = this.#capture;
    if (!capture) { this.#generation++; return Promise.resolve(null); }
    capture.stoppedAt ??= this.#now();
    this.#stopCapture(capture);
    return capture.done;
  }

  cancel() {
    this.#generation++;
    this.stopPlayback();
    const capture = this.#capture;
    this.#capture = null;
    if (!capture) return;
    this.#stopCapture(capture); capture.resolve(null);
  }

  #stopCapture(capture) {
    clearInterval(capture.timer);
    if (capture.recorder.state !== 'inactive') capture.recorder.stop();
    capture.release();
  }

  async play(key, { onEnded = () => {}, onError = () => {} } = {}) {
    this.cancel();
    const clip = this.clip(key);
    if (!clip) throw new CourseError('MIC', '本句还没有录音，请先录一句。');
    const audio = this.#audioFactory();
    this.#audio = audio;
    audio.src = clip.url;
    audio.onended = () => { if (this.#audio === audio) { this.stopPlayback(); onEnded(); } };
    audio.onerror = () => { if (this.#audio === audio) { this.stopPlayback(); onError(new CourseError('AUDIO', '录音暂时无法回听，请重录或更换浏览器。')); } };
    try {
      await audio.play();
      if (this.#audio !== audio) throw new DOMException('Playback cancelled', 'AbortError');
    } catch (error) { if (this.#audio === audio) this.stopPlayback(); throw error; }
  }

  stopPlayback() {
    if (!this.#audio) return;
    const audio = this.#audio; this.#audio = null;
    audio.onended = audio.onerror = null;
    audio.pause(); audio.removeAttribute('src'); audio.load();
  }

  delete(key) {
    this.cancel();
    const clip = this.clip(key);
    if (clip) this.#urls.revokeObjectURL(clip.url);
    this.#clips.delete(key);
  }

  retain(keys) {
    for (const key of this.#clips.keys()) if (!keys.includes(key)) this.delete(key);
  }

  clear() {
    this.cancel();
    for (const clip of this.#clips.values()) this.#urls.revokeObjectURL(clip.url);
    this.#clips.clear();
  }
}
