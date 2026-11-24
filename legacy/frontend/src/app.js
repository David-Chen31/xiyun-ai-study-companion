import { CourseError, courseOptions, createCourseClient, PracticeSession, PracticeBookmark } from './course.js';
import { ApiAudioProvider, DemoAudioProvider, PhrasePlayer, VoiceRecorder } from './audio.js';
import { PracticeView } from './view.js';
import { AccountProgress } from './progress.js';

// Compose the course, media and view; each collaborator owns its resources.
export class PracticeApp {
  #repository;
  #session;
  #player;
  #view;
  #recorder;
  #operation = null;
  #epoch = 0;
  #unavailable = false;
  #mounted = false;
  #disposed = false;
  #options = { rate: 1, repeats: 1 };
  #recording = { phase: 'idle', message: '', elapsed: 0 };
  #bookmark;
  #accountProgress;
  #accessCheck = null;
  #savedPhraseId = null;
  #restoreBookmark = true;
  #bookmarkMessage = '';

  constructor({ repository, session, player, view, recorder = new VoiceRecorder(), bookmark = new PracticeBookmark(), accountProgress }) {
    this.#repository = repository; this.#session = session;
    this.#player = player; this.#view = view; this.#recorder = recorder;
    this.#bookmark = bookmark;
    this.#accountProgress = accountProgress;
  }

  mount() {
    if (this.#mounted || this.#disposed) return;
    this.#mounted = true;
    this.#view.bind({
      select: id => this.select(id), play: () => this.play(),
      alternative: () => this.playAlternative(),
      question: text => this.ask(text),
      stop: () => this.stop(), retry: () => this.load(),
      rate: rate => this.setRate(rate), repeats: repeats => this.setRepeats(repeats),
      record: () => this.record(), stopRecording: () => this.stopRecording(),
      playRecording: () => this.playRecording(), deleteRecording: () => this.deleteRecording(),
      mark: () => this.markBookmark(), clearBookmark: () => this.clearBookmark(),
      online: () => this.#accountProgress?.flush(),
      hide: () => this.stop('已停止。返回页面后可以重新试听；未完成的录音已丢弃。'),
      show: () => this.load(), leave: () => this.suspend()
    });
    return this.load();
  }

  #current() {
    const epoch = this.#epoch;
    return () => epoch === this.#epoch && !this.#disposed;
  }
  #guard(action, current = this.#current()) {
    return (...args) => { if (current()) return action(...args); };
  }
  #idle(message) {
    this.#view.setPlayback({ phase: this.#unavailable ? 'unavailable' : 'idle', canPlay: !this.#unavailable && !!this.#session.current, message });
    this.#renderBookmark();
  }
  #renderBookmark() {
    this.#view.setBookmark({
      savedId: this.#savedPhraseId, course: this.#session.course,
      canMark: !!this.#session.current && !this.#unavailable,
      marked: !!this.#session.current && this.#session.current.id === this.#savedPhraseId,
      message: (this.#session.course?.progress ? '' : this.#bookmark.message) || this.#bookmarkMessage
    });
  }
  #renderRecording() {
    this.#view.setRecording({ ...this.#recording, clip: this.#recorder.clip(this.#session.key()), canRecord: !!this.#session.current && !this.#unavailable });
  }
  #recordState(phase, message, elapsed = 0) {
    this.#recording = { phase, message, elapsed };
    this.#renderRecording();
  }

  setRate(rate) {
    this.#player.setRate(rate);
    this.#options.rate = rate;
    this.#view.setOptions({ ...this.#options });
  }
  setRepeats(repeats) {
    this.#options.repeats = repeats;
    this.stop('播放方式已更新，点击试听开始。');
  }

  stop(message = '', completed = false) {
    this.#epoch++;
    if (this.#accessCheck) {
      clearTimeout(this.#accessCheck.timer);
      this.#accessCheck.controller?.abort();
      this.#accessCheck = null;
    }
    if (this.#operation) {
      clearTimeout(this.#operation.timer);
      this.#operation.controller.abort();
      this.#operation = null;
    }
    this.#player.stop(); this.#recorder.cancel();
    this.#recording = { phase: 'idle', elapsed: 0, message: this.#recording.phase === 'idle' ? this.#recording.message : '未完成的录音已取消，可以重新录一句。' };
    if (!completed) this.#view.resetProgress(this.#session.current);
    this.#view.setRetryBusy(false);
    this.#idle(message);
    this.#view.setOptions({ ...this.#options, round: 0 });
    this.#renderRecording();
  }

  // One lifecycle for course loading and audio preparation, including stale results.
  async #request(kind, action) {
    const audio = kind === 'audio';
    const message = audio ? '正在准备示范…' : '';
    this.stop();
    const controller = new AbortController(), current = this.#current();
    const timer = setTimeout(() => controller.abort(), audio ? 15000 : 10000);
    this.#operation = { controller, timer };
    this.#view.setPlayback({ phase: 'loading', canStop: audio, message });
    this.#view.setRetryBusy(!audio);
    try { await action({ signal: controller.signal, current }); }
    catch (error) {
      if (!current()) return;
      if (error.name === 'AbortError') error = new CourseError('NETWORK', audio ? '音频加载超时，请重试。' : '加载超时，请检查网络后重试。');
      else if (audio && !(error instanceof CourseError)) error = new CourseError(error.code || 'AUDIO', '音频暂时无法播放，请重试或联系老师。');
      this.#showError(error, audio);
    } finally {
      clearTimeout(timer);
      if (current()) { this.#operation = null; this.#view.setRetryBusy(false); }
    }
  }

  async #readCourse(operation) {
    const data = await this.#repository.load(operation.signal);
    if (!operation.current()) return false;
    const previousKey = this.#session.key();
    const restore = this.#session.replace(data) || this.#restoreBookmark;
    const accountBookmark = this.#accountProgress?.update(this.#session.course);
    this.#savedPhraseId = data.progress && this.#accountProgress
      ? accountBookmark : this.#bookmark.read(this.#session.course);
    if (restore) this.#session.select(this.#savedPhraseId ?? this.#session.course.phrases[0]?.id);
    this.#bookmarkMessage = restore && this.#savedPhraseId ? `已接着上次标记，选中第 ${this.#session.index + 1} 句。` : '';
    this.#restoreBookmark = false;
    this.#recorder.retain(this.#session.keys);
    if (previousKey !== this.#session.key()) this.#recording.message = '';
    this.#unavailable = false;
    this.#view.clearNotice(); this.#view.render(this.#session);
    this.#view.resetProgress(this.#session.current); this.#renderRecording(); this.#renderBookmark();
    return true;
  }

  #showError(error, audio = false) {
    const login = error.code === 'LOGIN_REQUIRED';
    const unavailable = login || ['COURSE_GONE', 'COURSE_UNKNOWN', 'MEDIA_FORBIDDEN'].includes(error.code);
    this.#unavailable ||= unavailable;
    this.stop();
    if (unavailable) {
      this.#recorder.clear();
      this.#renderRecording();
    } else if (audio && this.#session.current) {
      this.#view.setPlayback({phase:'error', canPlay:true, message:error.message});
      return;
    }
    const title = !unavailable ? '加载没有完成' : error.code === 'COURSE_GONE' ? '这份作业暂不能练习' : '暂时无法打开作业';
    this.#view.showNotice(title, error.message || '请稍后重试。', !unavailable, login, unavailable);
    if (!this.#session.course) this.#view.showUnloaded();
  }

  select(id) {
    if (this.#disposed) return;
    this.stop();
    if (!this.#session.select(id)) return;
    this.#bookmarkMessage = '';
    this.#view.renderSelection(this.#session); this.#view.resetProgress(this.#session.current);
    this.#recordState('idle', '');
    this.#idle();
  }

  async load() {
    if (this.#disposed) return;
    return this.#request('course', async operation => {
      if (await this.#readCourse(operation)) this.#idle();
    });
  }

  async play(round = 1) {
    if (this.#disposed || this.#unavailable || !this.#session.current) return;
    const requestedKey = this.#session.key();
    return this.#request('audio', async operation => {
      await this.#player.unlock();
      if (!operation.current() || !await this.#readCourse(operation)) return;
      if (!this.#session.current) { this.#idle('当前作业没有可练的句子。'); return; }
      if (round > 1 && requestedKey !== this.#session.key()) { this.#idle('作业已更新，循环已停止，请重新选择后试听。'); return; }
      await this.#player.play(this.#session.course, this.#session.current, {
        signal: operation.signal,
        onWaveform: this.#guard(peaks => this.#view.showWaveform?.(this.#session.key(), peaks), operation.current),
        onProgress: this.#guard(progress => this.#view.updateProgress(this.#session.current, progress), operation.current),
        onEnded: this.#guard(() => {
          if (round < this.#options.repeats) { void this.play(round + 1); return; }
          this.stop('', true);
          this.#view.updateProgress(this.#session.current, { positionMs: this.#session.current.end_ms, ratio: 1 }, true);
        }, operation.current)
      });
      if (!operation.current()) return;
      this.#view.setPlayback({ phase: 'playing', canStop: true });
      this.#view.setOptions({ ...this.#options, round });
      if (this.#session.course.class_course) this.#monitorAccess(operation.current);
    });
  }

  async playAlternative() {
    if (this.#disposed || this.#unavailable || !this.#session.current?.alternate_course_id) return;
    return this.#request('audio', async operation => {
      await this.#player.unlock();
      if (!operation.current() || !await this.#readCourse(operation)) return;
      const phraseId = this.#session.current?.id;
      if (!phraseId) return;
      const lookup = signal => this.#repository.alternative(phraseId, signal);
      const data = await lookup(operation.signal);
      if (!operation.current()) return;
      const alternate = data.alternative, phrase = alternate?.phrases[0];
      if (!phrase) { this.#idle(data.reason); return; }
      await this.#player.play(alternate, phrase, {
        signal: operation.signal,
        onEnded: this.#guard(() => this.stop('对照版本已播完，可以试听原作业。'), operation.current)
      });
      if (!operation.current()) return;
      this.#view.setPlayback({ phase:'playing', canPlay:true, canStop:true,
        message:`对照版本「${alternate.title}」 · ${alternate.credit} · ${phrase.text}${phrase.role ? ' · ' + phrase.role : ''}` });
      this.#monitorAccess(operation.current, async signal => {
        const result = await lookup(signal);
        if (!result.alternative) throw new CourseError('ALTERNATIVE_GONE', result.reason);
      });
    });
  }

  async ask(text) {
    if (!this.#session.course?.class_course) throw new Error('请先打开班级作业。');
    const response = await fetch('/v1/questions', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body:JSON.stringify({course_id:this.#session.course.course_id, question:text}),
      signal:AbortSignal.timeout(8000)
    });
    if (!response.ok) throw new Error('无法读取此作业的释义，请刷新或重新登录。');
    return response.json();
  }

  #monitorAccess(current, verify = signal => this.#repository.load(signal)) {
    const check = {timer: null, controller: null};
    const poll = async () => {
      const controller = check.controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 2500);
      try {
        await verify(controller.signal);
        if (current()) check.timer = setTimeout(poll, 2500);
      } catch (error) {
        if (current()) {
          if (error.code === 'ALTERNATIVE_GONE') this.stop(error.message);
          else this.#showError(error.name === 'AbortError'
              ? new CourseError('NETWORK', '作业权限复核超时，试听已暂停。') : error, true);
        }
      } finally {
        clearTimeout(timeout);
      }
    };
    this.#accessCheck = check;
    check.timer = setTimeout(poll, 2500);
  }

  async record() {
    if (this.#disposed || this.#unavailable || !this.#session.current) return;
    this.stop(); this.#recordState('requesting', '');
    const failed = this.#guard(error => this.#recordState('idle', error instanceof CourseError ? error.message : ({
      NotAllowedError: '未获得麦克风权限。可在地址栏的站点设置中允许后重试，也可继续听示范。',
      NotFoundError: '没有找到麦克风，连接设备后可重试，仍可听示范。',
      NotReadableError: '麦克风无法使用，请关闭占用麦克风的程序后重试。'
    })[error.name] || '无法录音，请检查麦克风和浏览器权限，仍可听示范。'));
    try {
      await this.#recorder.start(this.#session.key(), {
        onTick: this.#guard(elapsed => this.#recordState('recording', '', elapsed)),
        onComplete: this.#guard(() => this.#recordState('idle', '录音已完成。')),
        onError: failed
      });
    } catch (error) { if (error.name !== 'AbortError') failed(error); }
  }

  async stopRecording() {
    if (this.#recording.phase === 'requesting') { this.stop(); this.#recordState('idle', '已取消录音。'); return; }
    if (this.#recording.phase !== 'recording') return;
    this.#recordState('saving', '');
    await this.#recorder.stop();
  }

  async playRecording() {
    const clip = this.#recorder.clip(this.#session.key());
    if (this.#disposed || this.#unavailable || !clip) return;
    const phraseId = this.#session.current.id;
    this.stop();
    const current = this.#current(), finish = this.#guard(message => this.stop(message), current);
    try {
      await this.#recorder.play(this.#session.key(), {
        onEnded: () => {
          if (current()) this.#accountProgress?.complete(clip, phraseId);
          finish('本句录音已播完。');
        },
        onError: error => finish(error.message)
      });
      if (current()) this.#view.setPlayback({ phase: 'self', canPlay: !this.#unavailable, canStop: true });
    } catch (error) { if (error.name !== 'AbortError') finish('录音暂时无法回听，请重试或重新录音。'); }
  }

  deleteRecording() {
    this.stop(); this.#recorder.delete(this.#session.key());
    this.#recordState('idle', '本句录音已删除。');
  }
  markBookmark() {
    if (this.#disposed || this.#unavailable || !this.#session.current) return;
    const saved = this.#session.course.progress && this.#accountProgress
      ? this.#accountProgress.record('mark', this.#session.current.id)
      : this.#bookmark.save(this.#session.course, this.#session.current.id);
    if (saved) {
      this.#savedPhraseId = this.#session.current.id;
      this.#bookmarkMessage = '';
    }
    this.#renderBookmark();
  }
  clearBookmark() {
    if (this.#disposed || !this.#session.course) return;
    const cleared = this.#session.course.progress && this.#accountProgress && this.#session.current
      ? this.#accountProgress.record('clear', this.#session.current.id)
      : this.#bookmark.clear(this.#session.course);
    if (cleared) {
      this.#savedPhraseId = null;
      this.#bookmarkMessage = '待练记录已清除，下次打开从第一句开始。';
    }
    this.#renderBookmark();
  }
  async suspend() {
    this.stop('已暂停。返回页面后可以重新试听。');
    this.#restoreBookmark = true;
    this.#recorder.clear(); this.#recordState('idle', '');
    await this.#player.close();
  }
  async dispose() {
    if (this.#disposed) return;
    this.#disposed = true; this.stop(); this.#recorder.clear();
    this.#view.unbind(); await this.#player.close();
  }
}

// Source subclasses are interchangeable; importing this module never mounts a page.
export function startPractice({ document: pageDocument = globalThis.document, window: pageWindow = globalThis.window, location: pageLocation = globalThis.location } = {}) {
  const options = courseOptions(pageLocation.search, pageLocation.origin);
  const app = new PracticeApp({
    repository: createCourseClient(options), session: new PracticeSession(options.origin),
    bookmark: new PracticeBookmark({ storage: () => pageWindow.localStorage, scope: options.demo ? 'demo' : 'course' }),
    accountProgress: options.demo ? undefined : new AccountProgress(pageWindow),
    player: new PhrasePlayer({ provider: options.demo ? new DemoAudioProvider() : new ApiAudioProvider() }),
    view: new PracticeView({ document: pageDocument, window: pageWindow })
  });
  app.mount();
  return app;
}
