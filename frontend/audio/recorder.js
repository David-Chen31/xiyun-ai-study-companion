// One temporary recording at a time. The studio manages playback and file URLs.
export class VoiceRecorder {
  #capture = null;
  #generation = 0;

  async start({onTick, onComplete, onError, onInterrupted}) {
    this.clear();
    if (!globalThis.MediaRecorder) throw new Error('当前浏览器不支持录音。');
    const generation = this.#generation;
    const stream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    const release = () => stream.getTracks().forEach(track => track.stop());
    if (generation !== this.#generation) {
      release();
      throw new DOMException('Recording cancelled', 'AbortError');
    }
    let capture;
    try {
      const recorder = new MediaRecorder(stream);
      let resolve;
      const done = new Promise(complete => { resolve = complete; });
      capture = {recorder, release, chunks:[], startedAt:performance.now(), done, resolve};
      this.#capture = capture;
      recorder.ondataavailable = event => { if (event.data.size) capture.chunks.push(event.data); };
      recorder.onstop = () => {
        this.#stopCapture(capture);
        if (this.#capture !== capture) { resolve(null); return; }
        this.#capture = null;
        const blob = new Blob(capture.chunks, {type:recorder.mimeType || capture.chunks[0]?.type});
        if (!blob.size) {
          resolve(null);
          onError(new Error('没有录到声音，请检查麦克风后重录。'));
          return;
        }
        resolve(blob);
        onComplete(blob);
      };
      recorder.onerror = () => {
        if (this.#capture !== capture) return;
        this.clear();
        onError(new Error('录音被设备中断。'));
      };
      stream.getTracks().forEach(track => {
        track.onended = () => {
          if (this.#capture !== capture) return;
          this.clear();
          onInterrupted();
        };
      });
      recorder.start();
      onTick(0);
      capture.timer = setInterval(() => { if (this.#capture === capture) onTick(performance.now() - capture.startedAt); }, 200);
    } catch (error) {
      if (capture) {
        this.#capture = null;
        clearInterval(capture.timer);
        capture.resolve(null);
      }
      release();
      throw error;
    }
  }

  stop() {
    const capture = this.#capture;
    if (!capture) { this.#generation++; return Promise.resolve(null); }
    this.#stopCapture(capture);
    return capture.done;
  }

  clear() {
    this.#generation++;
    const capture = this.#capture;
    this.#capture = null;
    if (!capture) return;
    this.#stopCapture(capture);
    capture.resolve(null);
  }

  #stopCapture(capture) {
    clearInterval(capture.timer);
    if (capture.recorder.state !== 'inactive') capture.recorder.stop();
    capture.release();
  }
}
