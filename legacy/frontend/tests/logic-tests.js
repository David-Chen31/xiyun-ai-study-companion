import { CourseError, courseOptions, validateCourse, createCourseClient, selectedPhrase, playbackWindow } from '../src/course.js';
import { createDemoWav } from '../src/audio.js';
import { ApiCourseRepository, DemoCourseRepository } from '../src/course.js';
import { PracticeSession, PracticeBookmark } from '../src/course.js';
import { ApiAudioProvider, DemoAudioProvider } from '../src/audio.js';
import { PhrasePlayer, VoiceRecorder } from '../src/audio.js';
import { PracticeApp } from '../src/app.js';
import { PracticeView } from '../src/view.js';
import { compare, findActivity } from '../personal/analysis.js';

// Execute real app classes with controllable platform fakes; never request a microphone.
function equal(actual, expected) {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) throw new Error(`预期 ${JSON.stringify(expected)}，收到 ${JSON.stringify(actual)}`);
}
function throws(run, code) {
  try { run(); } catch (error) { equal(error.code, code); return; }
  throw new Error(`预期错误 ${code}，但操作成功了`);
}
async function rejects(run, code) {
  try { await run(); } catch (error) { equal(error.code, code); return; }
  throw new Error(`预期错误 ${code}，但操作成功了`);
}

function deferred() {
  let resolve;
  const promise = new Promise(yes => { resolve = yes; });
  return { promise, resolve };
}

class TestView {
  playback = {}; notice = null; renderCount = 0; bindCount = 0; unbindCount = 0;
  bind(actions) { this.bindCount++; this.actions = actions; }
  unbind() { this.unbindCount++; this.actions = null; }
  setPlayback(state) { this.playback = state; }
  setOptions(state) { this.options = state; }
  setRecording(state) { this.recording = state; }
  setBookmark(state) { this.bookmark = state; }
  resetProgress(phrase) { this.progress = { positionMs: phrase?.lead_ms ?? 0, ratio: 0 }; }
  updateProgress(phrase, progress, completed = false) { this.progress = progress; this.completed = completed; }
  setRetryBusy(value) { this.retryBusy = value; }
  showNotice(title, description, retry) { this.notice = { title, description, retry }; }
  clearNotice() { this.notice = null; }
  showUnloaded() { this.unloaded = true; }
  render(session) {
    this.renderCount++; this.title = session.course.title;
    this.currentId = session.current?.id ?? null;
    if (!session.current) this.showNotice('没有可练的句子', '', false);
  }
  renderSelection(session) { this.currentId = session.current?.id ?? null; }
  selectionMessage(session) { return `selected ${session.current.id}`; }
  playingMessage(phrase) { return `playing ${phrase.id}`; }
}

class TestPlayer {
  calls = []; stopped = 0; closed = 0;
  async unlock() {}
  setRate(rate) { this.rate = rate; }
  async play(course, phrase, options) { this.calls.push(phrase.id); this.options = options; }
  stop() { this.stopped++; }
  async close() { this.closed++; }
  end() { this.options.onEnded(); }
}

function testAudioContext(duration = 12) {
  return {
    state: 'suspended', currentTime: 0, destination: {}, nodes: [],
    async resume() { this.state = 'running'; },
    async decodeAudioData() { return { duration }; },
    async close() { this.state = 'closed'; },
    createBufferSource() {
      const node = {
        playbackRate: { value: 1 },
        stopCount: 0, disconnectCount: 0,
        connect() {}, disconnect() { this.disconnectCount++; },
        start(...args) { this.args = args; }, stop() { this.stopCount++; }
      };
      this.nodes.push(node); return node;
    }
  };
}

function memoryStorage() {
  const values = new Map();
  return { values, getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key) };
}

function harness(fixture, origin, repository, player = new TestPlayer(), recorder, bookmark) {
  const view = new TestView(), session = new PracticeSession(origin);
  const storage = memoryStorage();
  const app = new PracticeApp({ repository: repository || { load: async () => structuredClone(fixture) }, session, player, view, recorder, bookmark: bookmark || new PracticeBookmark({ storage: () => storage }) });
  return { app, view, session, player };
}

function testFrames() {
  let sequence = 0;
  const pending = new Map();
  return {
    pending,
    requestFrame: callback => { const id = ++sequence; pending.set(id, callback); return id; },
    cancelFrame: id => pending.delete(id),
    step() {
      const [id, callback] = pending.entries().next().value;
      pending.delete(id); callback();
    }
  };
}

function voiceHarness(getUserMedia) {
  let clock = 0;
  const tracks = [], recorders = [], audios = [], revoked = [], blobs = [];
  class Recorder {
    state = 'inactive'; mimeType = 'audio/webm';
    constructor() { recorders.push(this); }
    start() { this.state = 'recording'; }
    stop() {
      this.state = 'inactive';
      queueMicrotask(() => {
        this.ondataavailable({ data: new Blob(['recorded voice']) });
        this.onstop();
      });
    }
  }
  const stream = () => {
    const track = { stopped: false, stop() { this.stopped = true; } };
    tracks.push(track); return { getTracks: () => [track] };
  };
  const recorder = new VoiceRecorder({
    getUserMedia: getUserMedia || (async constraints => { equal(constraints, { audio: true, video: false }); return stream(); }),
    recorderClass: Recorder, now: () => clock,
    urls: { createObjectURL: blob => { blobs.push(blob); return `blob:test-${blobs.length}`; }, revokeObjectURL: url => revoked.push(url) },
    audioFactory: () => {
      const audio = {
        playing: false,
        async play() { this.playing = true; }, pause() { this.playing = false; },
        removeAttribute() { this.src = ''; }, load() {}
      };
      audios.push(audio); return audio;
    }
  });
  return { recorder, tracks, recorders, audios, revoked, blobs, stream, time: value => { clock = value; } };
}

async function until(check) {
  for (let i = 0; i < 30; i++) { if (check()) return; await Promise.resolve(); }
  throw new Error('异步状态未完成');
}

export async function runLogicTests(fixture, origin = 'http://localhost:5173') {
  const tests = [];
  const test = (name, run) => tests.push({ name, run });

  test('根地址默认样例，真实作业链接始终请求 A 的 API', async () => {
    const sample = courseOptions('', origin);
    equal(sample.demo, true);
    const sampleClient = createCourseClient({ ...sample, fetcher: async path => {
      equal(path, './示例数据/三句作业.json');
      return new Response(JSON.stringify(fixture));
    } });
    equal((await sampleClient.load()).course_id, 'c1');
    equal(courseOptions('?scenario=gone', origin).scenario, 'gone');
    const actual = courseOptions('?course=c1', origin);
    equal(actual.demo, false);
    equal(courseOptions('?course=c1&demo=1', origin).demo, false);
    const realClient = createCourseClient({ ...actual, fetcher: async path => {
      equal(path, '/v1/courses/c1');
      return new Response('', { status: 410 });
    } });
    await rejects(() => realClient.load(), 'COURSE_GONE');
  });

  test('三句顺序、空入点与第二句 4–8 秒', () => {
    const course = validateCourse(structuredClone(fixture), origin);
    equal(course.phrases.map(phrase => phrase.id), ['p1', 'p2', 'p3']);
    equal(course.phrases[2].entry_ms, null);
    equal(playbackWindow(course.phrases[1]), { offset: 4, duration: 4 });
  });
  test('保留当前句、句子不存在时回到首句、空作业', () => {
    equal(selectedPhrase(fixture, 'p2').id, 'p2');
    equal(selectedPhrase(fixture, 'deleted').id, 'p1');
    equal(selectedPhrase({ phrases: [] }, 'p2'), null);
  });
  test('拒绝非法时轴、重复句号和跨域媒体', () => {
    for (const mutate of [data => data.phrases[1].end_ms = 13000, data => data.phrases[1].entry_ms = 8000, data => data.phrases[1].lead_ms = 5001, data => data.phrases[1].id = 'p1', data => data.stream_url = 'https://other.example/audio.wav']) {
      const data = structuredClone(fixture); mutate(data);
      throws(() => validateCourse(data, origin), 'INVALID_COURSE');
    }
  });
  test('真实模式只请求 A 的公开地址，使用 no-store', async () => {
    const calls = [];
    const client = createCourseClient({ origin, courseId: 'c1', fetcher: async (...args) => { calls.push(args); return new Response(JSON.stringify(fixture)); } });
    equal((await client.load()).course_id, 'c1');
    equal(calls.length, 1);
    equal(calls[0][0], '/v1/courses/c1');
    equal(calls[0][1].cache, 'no-store');
  });
  test('410 / 404 / 403 / 503 不回退到样例', async () => {
    for (const [status, code] of [[410, 'COURSE_GONE'], [404, 'COURSE_UNKNOWN'], [403, 'MEDIA_FORBIDDEN'], [503, 'NETWORK']]) {
      let count = 0;
      const client = createCourseClient({ origin, courseId: 'c1', fetcher: async () => { count++; return new Response('', { status }); } });
      await rejects(() => client.load(), code);
      equal(count, 1);
    }
  });
  test('网络失败、坏 JSON 和错误作业 ID 被拒绝', async () => {
    for (const [fetcher, code] of [
      [async () => { throw new TypeError('offline'); }, 'NETWORK'],
      [async () => new Response('<html>bad</html>'), 'INVALID_COURSE'],
      [async () => new Response(JSON.stringify({ ...fixture, course_id: 'other' })), 'INVALID_COURSE']
    ]) await rejects(() => createCourseClient({ origin, courseId: 'c1', fetcher }).load(), code);
  });
  test('显式样例模式、空作业和撤回状态', async () => {
    const fetcher = async path => { equal(path, './示例数据/三句作业.json'); return new Response(JSON.stringify(fixture)); };
    equal((await createCourseClient({ origin, demo: true, fetcher }).load()).phrases.length, 3);
    equal((await createCourseClient({ origin, demo: true, scenario: 'empty', fetcher }).load()).phrases.length, 0);
    await rejects(() => createCourseClient({ origin, demo: true, scenario: 'gone', fetcher }).load(), 'COURSE_GONE');
  });
  test('测试音为 12 秒 16-bit 单声道 PCM', () => {
    const view = new DataView(createDemoWav());
    equal(view.getUint16(20, true), 1);
    equal(view.getUint16(22, true), 1);
    equal(view.getUint16(34, true), 16);
    equal(view.getUint32(40, true) / view.getUint32(28, true), 12);
  });

  test('同一 load 接口可替换样例与 API 数据源', async () => {
    const paths = [];
    const options = { origin, courseId: 'c1', fetcher: async path => { paths.push(path); return new Response(JSON.stringify(fixture)); } };
    for (const repository of [new DemoCourseRepository(options), new ApiCourseRepository(options)]) equal((await repository.load()).course_id, 'c1');
    equal(paths, ['./示例数据/三句作业.json', '/v1/courses/c1']);
  });

  test('封装浏览器请求时保留正确调用对象，课程和音频均可读取', async () => {
    const fetcher = function (path) {
      if (this !== globalThis) throw new TypeError('Illegal invocation');
      return Promise.resolve(path === fixture.stream_url
        ? new Response(new Uint8Array([1, 2, 3]))
        : new Response(JSON.stringify(fixture)));
    };
    const options = { origin, courseId: 'c1', fetcher };
    equal((await new DemoCourseRepository(options).load()).course_id, 'c1');
    equal((await new ApiCourseRepository(options).load()).course_id, 'c1');
    equal((await new ApiAudioProvider(fetcher).getBytes(fixture)).byteLength, 3);
  });

  test('状态保留独立副本，外部输入改动和无效选句不污染当前句', () => {
    const session = new PracticeSession(origin), input = structuredClone(fixture);
    session.replace(input); session.select('p2'); input.phrases[1].text = '外部改写';
    equal(session.current.text, fixture.phrases[1].text);
    equal(session.select('missing'), false); equal(session.current.id, 'p2');
  });

  test('同作业同修订才保留选句，接口无效数据在进入状态时被拒绝', async () => {
    const session = new PracticeSession(origin);
    session.replace(fixture); session.select('p2'); session.replace(structuredClone(fixture)); equal(session.current.id, 'p2');
    const repository = new ApiCourseRepository({ origin, courseId: 'c1', fetcher: async () => new Response(JSON.stringify({ ...fixture, duration_ms: 1 })) });
    const invalidData = await repository.load();
    throws(() => session.replace(invalidData), 'INVALID_COURSE'); equal(session.current.id, 'p2');
    session.replace({ ...fixture, revision_id: 'r2' }); equal(session.current.id, 'p1'); session.select('p2');
    session.replace({ ...fixture, course_id: 'other', revision_id: 'r2' }); equal(session.current.id, 'p1');
    session.replace({ ...fixture, phrases: [] }); equal(session.current, null);
  });

  test('统一音频源接口提供字节，真实音频 410 不回退', async () => {
    equal((await new DemoAudioProvider().getBytes(fixture)).byteLength, createDemoWav().byteLength);
    const controller = new AbortController();
    const provider = new ApiAudioProvider(async (path, options) => {
      equal(path, fixture.stream_url); equal(options.cache, 'no-store'); equal(options.signal === controller.signal, true);
      return new Response('', { status: 410 });
    });
    await rejects(() => provider.getBytes(fixture, controller.signal), 'COURSE_GONE');
  });

  test('播放器按 4 秒偏移播放 4 秒，换段停止旧音频，结束只通知一次', async () => {
    const context = testAudioContext();
    const player = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => context });
    let ended = 0;
    await player.play(fixture, fixture.phrases[1], { onEnded: () => ended++ }); equal(context.nodes[0].args, [0, 4, 4]);
    await player.play(fixture, fixture.phrases[0], { onEnded: () => ended++ });
    equal(context.nodes[0].stopCount, 1); equal(context.nodes[0].onended, null);
    const done = context.nodes[1].onended; done(); done(); equal(ended, 1);
    await player.close(); equal(context.state, 'closed');
  });

  test('等待音频时停止，迟到数据不会启动声音', async () => {
    const bytes = deferred(), entered = deferred(), context = testAudioContext();
    const player = new PhrasePlayer({ provider: { getBytes() { entered.resolve(); return bytes.promise; } }, contextFactory: () => context });
    const pending = player.play(fixture, fixture.phrases[1]);
    await entered.promise; player.stop(); bytes.resolve(createDemoWav());
    try { await pending; throw new Error('已取消的播放不应成功'); } catch (error) { equal(error.name, 'AbortError'); }
    equal(context.nodes.length, 0); await player.close();
  });

  test('解码中取消和音频过短均不会创建播放节点', async () => {
    const decoded = deferred(), entered = deferred(), context = testAudioContext();
    context.decodeAudioData = () => { entered.resolve(); return decoded.promise; };
    const player = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => context });
    const controller = new AbortController(), pending = player.play(fixture, fixture.phrases[1], { signal: controller.signal });
    await entered.promise; controller.abort(); decoded.resolve({ duration: 12 });
    try { await pending; throw new Error('已取消的播放不应成功'); } catch (error) { equal(error.name, 'AbortError'); }
    equal(context.nodes.length, 0); await player.close();
    const shortContext = testAudioContext(2), short = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => shortContext });
    await rejects(() => short.play(fixture, fixture.phrases[1]), 'AUDIO'); equal(shortContext.nodes.length, 0); await short.close();
  });

  test('组合课程、状态、播放器和视图完成选句、重验与试听', async () => {
    let reads = 0;
    const h = harness(fixture, origin, { async load() { reads++; return structuredClone(fixture); } });
    await h.app.mount(); h.app.select('p2'); await h.app.play();
    equal(reads, 2); equal(h.player.calls, ['p2']); equal(h.view.playback.phase, 'playing');
    h.player.end(); equal(h.view.playback.phase, 'idle'); equal(h.view.currentId, 'p2'); await h.app.dispose();
  });

  test('旧课程请求迟到不会覆盖新请求或提前解除加载状态', async () => {
    const first = deferred(), second = deferred(), signals = [];
    const h = harness(fixture, origin, { load(signal) { signals.push(signal); return signals.length === 1 ? first.promise : second.promise; } });
    const initial = h.app.mount(), fresh = h.app.load(); equal(signals[0].aborted, true);
    first.resolve({ ...fixture, title: '过时作业' }); await initial; equal(h.view.retryBusy, true); equal(h.view.renderCount, 0);
    second.resolve({ ...fixture, title: '最新作业' }); await fresh;
    equal(h.view.title, '最新作业'); equal(h.view.retryBusy, false); await h.app.dispose();
  });

  test('换句取消取音频操作，迟到音频不能覆盖新选句', async () => {
    const bytes = deferred(), entered = deferred(), context = testAudioContext();
    const player = new PhrasePlayer({ provider: { getBytes() { entered.resolve(); return bytes.promise; } }, contextFactory: () => context });
    const h = harness(fixture, origin, null, player);
    await h.app.mount(); h.app.select('p2'); const pending = h.app.play(); await entered.promise;
    h.app.select('p3'); bytes.resolve(createDemoWav()); await pending;
    equal(h.session.current.id, 'p3'); equal(h.view.playback.phase, 'idle'); equal(context.nodes.length, 0); await h.app.dispose();
  });

  test('撤回停止试听且切句不能绕过，重新取课成功才恢复', async () => {
    let gone = false;
    const h = harness(fixture, origin, { async load() { if (gone) throw new CourseError('COURSE_GONE', '已撤回'); return structuredClone(fixture); } });
    await h.app.mount(); h.app.select('p2'); gone = true; await h.app.play();
    equal(h.player.calls.length, 0); equal(h.view.playback.canPlay, false); h.app.select('p3'); equal(h.view.playback.canPlay, false);
    gone = false; await h.app.load(); equal(h.session.current.id, 'p3'); equal(h.view.playback.canPlay, true); await h.app.dispose();
  });

  test('临时网络失败保留选句并提供重试', async () => {
    let offline = false;
    const h = harness(fixture, origin, { async load() { if (offline) throw new Error('offline'); return structuredClone(fixture); } });
    await h.app.mount(); h.app.select('p2'); offline = true; await h.app.load();
    equal(h.session.current.id, 'p2'); equal(h.view.notice.retry, true); equal(h.view.retryBusy, false); await h.app.dispose();
  });

  test('播放前重验变为空作业时不播放，保留空状态说明', async () => {
    let empty = false;
    const h = harness(fixture, origin, { async load() { return { ...fixture, phrases: empty ? [] : fixture.phrases }; } });
    await h.app.mount(); empty = true; await h.app.play();
    equal(h.session.current, null); equal(h.view.playback.canPlay, false); equal(h.player.calls.length, 0); equal(!!h.view.notice, true); await h.app.dispose();
  });

  test('销毁取消请求、解绑事件且只释放一次，迟到结果不更新页面', async () => {
    const response = deferred(); let reads = 0;
    const h = harness(fixture, origin, { load() { reads++; return response.promise; } });
    const pending = h.app.mount(); h.app.mount(); equal(h.view.bindCount, 1);
    await h.app.dispose(); await h.app.dispose(); response.resolve(fixture); await pending; await h.app.load(); await h.app.play();
    equal(reads, 1); equal(h.view.renderCount, 0); equal(h.view.unbindCount, 1); equal(h.player.closed, 1);
  });

  test('播放进度使用音频时钟，从第二句 4 秒推进到 8 秒且不越界', async () => {
    const context = testAudioContext(), frames = testFrames(), reports = [];
    context.currentTime = 10;
    const player = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => context, requestFrame: frames.requestFrame, cancelFrame: frames.cancelFrame });
    await player.play(fixture, fixture.phrases[1], { onProgress: progress => reports.push(progress) });
    equal(reports[0], { positionMs: 4000, ratio: 0 });
    context.currentTime = 11; frames.step(); equal(reports.at(-1), { positionMs: 5000, ratio: .25 });
    context.currentTime = 12; frames.step(); equal(reports.at(-1), { positionMs: 6000, ratio: .5 });
    context.currentTime = 20; frames.step(); equal(reports.at(-1), { positionMs: 8000, ratio: 1 });
    equal(frames.pending.size, 0); context.nodes[0].onended(); await player.close();
  });

  test('停止取消进度帧，旧回调不会更新新播放或遗留动画任务', async () => {
    const context = testAudioContext(), frames = testFrames(), reports = [];
    const player = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => context, requestFrame: frames.requestFrame, cancelFrame: frames.cancelFrame });
    await player.play(fixture, fixture.phrases[1], { onProgress: progress => reports.push(progress) });
    const stale = frames.pending.values().next().value;
    player.stop(); equal(frames.pending.size, 0);
    await player.play(fixture, fixture.phrases[0], { onProgress: progress => reports.push(progress) });
    const before = reports.length;
    stale(); equal(reports.length, before); equal(frames.pending.size, 1);
    player.stop(); equal(frames.pending.size, 0); await player.close();
  });

  test('控制器转发进度，结束停在末端，换句后丢弃旧进度', async () => {
    const h = harness(fixture, origin);
    await h.app.mount(); h.app.select('p2'); await h.app.play();
    const oldProgress = h.player.options.onProgress;
    oldProgress({ positionMs: 6000, ratio: .5 }); equal(h.view.progress.ratio, .5);
    h.player.end(); equal(h.view.progress, { positionMs: 8000, ratio: 1 }); equal(h.view.completed, true);
    h.app.select('p3'); oldProgress({ positionMs: 7000, ratio: .75 }); equal(h.view.progress, { positionMs: 8100, ratio: 0 });
    await h.app.dispose();
  });

  test('真实视图更新播放线、时间和入点阶段，同时保留固定入点线', () => {
    const elements = new Map();
    const element = id => {
      if (!elements.has(id)) elements.set(id, { hidden: true, style: {}, textContent: '' });
      return elements.get(id);
    };
    element('score-entry').style.left = '27.5%';
    const view = new PracticeView({ document: { getElementById: element }, window: {}, demo: true });
    const phrase = fixture.phrases[1];
    view.resetProgress(phrase); equal(element('score-playhead').hidden, true);
    view.updateProgress(phrase, { positionMs: 5000, ratio: .25 });
    equal(element('score-playhead').style.left, '25%'); equal(element('score-current').textContent, '1.0 秒');
    equal(element('playback-stage').textContent, '前导伴奏'); equal(element('score-playhead').hidden, false);
    view.updateProgress(phrase, { positionMs: 6000, ratio: .5 });
    equal(element('score-playhead').style.left, '50%'); equal(element('playback-stage').textContent, '已到开口位置');
    equal(element('score-entry').style.left, '27.5%');
    view.updateProgress(phrase, { positionMs: 8000, ratio: 1 }, true); equal(element('playback-stage').textContent, '本句已播完');
    view.resetProgress(phrase); equal(element('score-playhead').hidden, true); equal(element('score-playhead').style.left, '0%');
  });

  test('两段颜色以入点分界，固定竖线与分界一致；无入点时不画入点线', () => {
    const elements = new Map();
    const element = id => {
      if (!elements.has(id)) elements.set(id, {
        hidden: false, textContent: '', querySelectorAll: () => [],
        classList: { toggle() {} }, style: { setProperty(key, value) { this[key] = value; } }
      });
      return elements.get(id);
    };
    const session = new PracticeSession(origin);
    session.replace(fixture);
    const view = new PracticeView({ document: { getElementById: element }, window: {}, demo: true });
    for (const id of ['p1', 'p2']) {
      session.select(id); view.renderSelection(session);
      equal(element('score-lead').style.width, element('score-entry').style.left);
      equal(element('score-entry').hidden, false);
    }
    equal(Math.abs(parseFloat(element('score-lead').style.width) - 27.5) < .0001, true);
    session.select('p3'); view.renderSelection(session);
    equal(element('score-entry').hidden, true); equal(element('score-boundary').textContent, '开口位置未标注');
  });

  test('每句从零显示时间，第二句开口在 1.1 秒、时长 4 秒，源坐标保持原值', () => {
    const elements = new Map();
    const element = id => {
      if (!elements.has(id)) elements.set(id, {
        hidden: false, textContent: '', querySelectorAll: () => [],
        classList: { toggle() {} }, style: { setProperty(key, value) { this[key] = value; } }
      });
      return elements.get(id);
    };
    const session = new PracticeSession(origin);
    session.replace(fixture); session.select('p2');
    const view = new PracticeView({ document: { getElementById: element }, window: {}, demo: true });
    view.renderSelection(session);
    equal(element('score-boundary').textContent, '开口 1.1 秒');
    equal(element('score-duration').textContent, '4 秒');
    view.resetProgress(session.current); equal(element('score-current').textContent, '0.0 秒');
    view.updateProgress(session.current, { positionMs: 6000, ratio: .5 }); equal(element('score-current').textContent, '2.0 秒');
    equal(session.current.lead_ms, 4000); equal(session.current.entry_ms, 5100); equal(session.current.end_ms, 8000);
  });

  test('0.75 倍速按源时间前进，播放中恢复原速时位置连续', async () => {
    const context = testAudioContext(), frames = testFrames(), reports = [];
    const player = new PhrasePlayer({ provider: new DemoAudioProvider(), contextFactory: () => context, requestFrame: frames.requestFrame, cancelFrame: frames.cancelFrame });
    player.setRate(.75);
    await player.play(fixture, fixture.phrases[1], { onProgress: progress => reports.push(progress) });
    equal(context.nodes[0].playbackRate.value, .75); equal(context.nodes[0].args, [0, 4, 4]);
    context.currentTime = 2; frames.step(); equal(reports.at(-1), { positionMs: 5500, ratio: .375 });
    player.setRate(1); frames.step(); equal(reports.at(-1).positionMs, 5500);
    context.currentTime = 3; frames.step(); equal(reports.at(-1).positionMs, 6500);
    context.currentTime = 20; frames.step(); equal(reports.at(-1), { positionMs: 8000, ratio: 1 });
    await player.close();
  });

  test('循环三遍只播当前句，每遍重新核验课程，第三遍结束不再播放', async () => {
    let reads = 0;
    const h = harness(fixture, origin, { async load() { reads++; return structuredClone(fixture); } });
    await h.app.mount(); h.app.select('p2'); h.app.setRate(.75); h.app.setRepeats(3); await h.app.play();
    equal(h.player.rate, .75); equal(h.view.options.round, 1);
    h.player.end(); await until(() => h.view.options.round === 2);
    h.player.end(); await until(() => h.view.options.round === 3);
    h.player.end(); await until(() => h.view.completed === true);
    equal(h.player.calls, ['p2', 'p2', 'p2']); equal(reads, 4); equal(h.view.playback.phase, 'idle');
    await h.app.dispose();
  });

  test('循环等待重验时停止或换句，旧请求不能重新启动播放', async () => {
    for (const action of ['stop', 'select']) {
      const pending = deferred(); let reads = 0;
      const h = harness(fixture, origin, { async load() { return ++reads < 3 ? structuredClone(fixture) : pending.promise; } });
      await h.app.mount(); h.app.select('p2'); h.app.setRepeats(3); await h.app.play();
      h.player.end(); await until(() => reads === 3);
      if (action === 'stop') h.app.stop(); else h.app.select('p3');
      pending.resolve(structuredClone(fixture));
      for (let i = 0; i < 10; i++) await Promise.resolve();
      equal(h.player.calls, ['p2']); equal(h.view.playback.phase, 'idle'); await h.app.dispose();
    }
  });

  test('循环中作业撤回或修订变化，停止循环且不串到新句', async () => {
    for (const change of ['gone', 'revision']) {
      let reads = 0;
      const h = harness(fixture, origin, { async load() {
        if (++reads < 3) return structuredClone(fixture);
        if (change === 'gone') throw new CourseError('COURSE_GONE', '已撤回');
        return { ...fixture, revision_id: 'r2' };
      } });
      await h.app.mount(); h.app.select('p2'); h.app.setRepeats(3); await h.app.play(); h.player.end();
      await until(() => change === 'gone' ? h.view.playback.phase === 'unavailable' : h.session.course.revision_id === 'r2');
      equal(h.player.calls, ['p2']); await h.app.dispose();
    }
  });

  test('录音结束立即释放麦克风，生成本句临时回听且时长正确', async () => {
    const v = voiceHarness(); let completed;
    await v.recorder.start('p2', { onComplete: clip => { completed = clip; } });
    v.time(2400); const finished = v.recorder.stop(); equal(v.tracks[0].stopped, true);
    await finished; equal(completed.durationMs, 2400); equal(completed.take, 1); equal(v.blobs.length, 1);
    await v.recorder.play('p2'); equal(v.audios[0].playing, true);
    let ended = false; await v.recorder.play('p2', { onEnded: () => { ended = true; } });
    equal(v.audios[0].playing, false); v.audios[1].onended(); equal(ended, true); equal(v.audios[1].playing, false);
    v.recorder.clear(); equal(v.revoked, ['blob:test-1']);
  });

  test('重录覆盖本句且释放旧音频，其他句独立；删除和清理释放全部临时 URL', async () => {
    const v = voiceHarness();
    await v.recorder.start('p1'); await v.recorder.stop();
    await v.recorder.start('p2'); await v.recorder.stop();
    await v.recorder.start('p1'); await v.recorder.stop();
    equal(v.recorder.clip('p1').take, 2); equal(v.recorder.clip('p2').take, 1); equal(v.revoked, ['blob:test-1']);
    await v.recorder.play('p1'); v.recorder.delete('p1'); equal(v.audios[0].playing, false); equal(v.recorder.clip('p1'), undefined);
    v.recorder.retain([]); equal(v.recorder.clip('p2'), undefined); equal(v.revoked.length, 3);
  });

  test('取消权限等待后迟到的麦克风立即释放，不创建录音或回听文件', async () => {
    const permission = deferred(), v = voiceHarness(() => permission.promise);
    const pending = v.recorder.start('p2'); v.recorder.cancel(); permission.resolve(v.stream());
    try { await pending; throw new Error('取消后不应录音'); } catch (error) { equal(error.name, 'AbortError'); }
    equal(v.tracks[0].stopped, true); equal(v.recorders.length, 0); equal(v.blobs.length, 0);
  });

  test('取消正在录音，迟到的 dataavailable/stop 不能保存或恢复录音', async () => {
    const v = voiceHarness(); let completed = false;
    await v.recorder.start('p2', { onComplete: () => { completed = true; } }); v.recorder.cancel();
    await Promise.resolve(); equal(v.tracks[0].stopped, true); equal(completed, false); equal(v.blobs.length, 0);
  });

  test('麦克风拒权后仍能试听，允许重试并完成录音', async () => {
    let deny = true; const v = voiceHarness(async () => { if (deny) throw new DOMException('Denied', 'NotAllowedError'); return v.stream(); });
    const h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.record(); equal(h.view.recording.phase, 'idle');
    equal(h.view.recording.message.includes('权限'), true); equal(h.view.playback.canPlay, true);
    await h.app.play(); equal(h.player.calls, ['p1']); deny = false;
    await h.app.record(); equal(h.view.recording.phase, 'recording'); equal(h.view.playback.phase, 'idle');
    v.time(1200); await h.app.stopRecording(); equal(h.view.recording.clip.durationMs, 1200); await h.app.dispose();
  });

  test('已有录音重录被拒权时保留原录音，设备录音错误释放麦克风', async () => {
    let deny = false; const v = voiceHarness(async () => { if (deny) throw new DOMException('Denied', 'NotAllowedError'); return v.stream(); });
    const h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.record(); await h.app.stopRecording();
    const previous = h.view.recording.clip.url;
    deny = true; await h.app.record(); equal(h.view.recording.clip.url, previous); equal(v.revoked.length, 0);
    deny = false; await h.app.record(); v.recorders.at(-1).onerror(); await Promise.resolve();
    equal(v.tracks.at(-1).stopped, true); equal(h.view.recording.phase, 'idle'); equal(h.view.recording.clip.url, previous);
    await h.app.dispose();
  });

  test('录音器构造失败也释放已授权的麦克风，不支持录音时不请求权限', async () => {
    let stopped = false, requested = false;
    class BrokenRecorder { constructor() { throw new Error('device failure'); } }
    const recorder = new VoiceRecorder({ recorderClass: BrokenRecorder, getUserMedia: async () => ({ getTracks: () => [{ stop() { stopped = true; } }] }) });
    try { await recorder.start('p1'); throw new Error('不应成功'); } catch (error) { equal(error.message, 'device failure'); }
    equal(stopped, true);
    const unsupported = new VoiceRecorder({ recorderClass: null, getUserMedia: async () => { requested = true; } });
    await rejects(() => unsupported.start('p1'), 'MIC'); equal(requested, false);
  });

  test('真实录音视图反映录制、结束、重录和删除按钮状态', () => {
    const elements = new Map();
    const element = id => {
      if (!elements.has(id)) elements.set(id, { textContent: '', classList: { toggle() {} } });
      return elements.get(id);
    };
    const view = new PracticeView({ document: { getElementById: element }, window: {}, demo: true });
    view.setRecording({ phase: 'requesting', canRecord: true });
    equal(element('stop-recording').textContent, '取消录音'); equal(element('record').disabled, true);
    view.setRecording({ phase: 'recording', canRecord: true, elapsed: 1500 });
    equal(element('recording-clock').textContent, '1.5 秒'); equal(element('stop-recording').disabled, false);
    view.setRecording({ phase: 'saving', canRecord: true }); equal(element('stop-recording').disabled, true);
    view.setRecording({ phase: 'idle', canRecord: true, clip: { take: 2, durationMs: 2100 } });
    equal(element('record-label').textContent, '再录一次'); equal(element('play-recording').hidden, false); equal(element('recording-take').textContent, '本句第 2 遍 · 2.1 秒');
    view.setRecording({ phase: 'idle', canRecord: true });
    equal(element('delete-recording').hidden, true); equal(element('play-recording').hidden, true); equal(element('record-label').textContent, '录这一句');
  });

  test('听自己和示范交替播放，换句显示对应录音，重录后显示遍数', async () => {
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.record(); await h.app.stopRecording(); await h.app.playRecording();
    equal(h.view.playback.phase, 'self'); equal(v.audios[0].playing, true);
    await h.app.play(); equal(v.audios[0].playing, false); equal(h.view.playback.phase, 'playing');
    await h.app.playRecording(); equal(h.view.playback.phase, 'self');
    h.app.select('p2'); equal(v.audios[1].playing, false); equal(h.view.recording.clip, undefined);
    h.app.select('p1'); equal(h.view.recording.clip.take, 1);
    await h.app.record(); await h.app.stopRecording(); equal(h.view.recording.clip.take, 2);
    h.app.deleteRecording(); equal(h.view.recording.clip, undefined); await h.app.dispose();
  });

  test('切后台丢弃未完成录音，离页清除已录内容并停止回听', async () => {
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.record(); h.view.actions.hide(); await Promise.resolve();
    equal(v.tracks[0].stopped, true); equal(h.view.recording.phase, 'idle'); equal(h.view.recording.clip, undefined);
    await h.app.record(); await h.app.stopRecording(); await h.app.playRecording();
    await h.app.suspend(); equal(v.audios[0].playing, false); equal(h.view.recording.clip, undefined); equal(v.revoked.length, 1);
    await h.app.dispose();
  });

  test('作业修订改变后不能把旧录音当成新作业的本句回听', async () => {
    let revision = 'r1'; const v = voiceHarness();
    const h = harness(fixture, origin, { async load() { return { ...fixture, revision_id: revision }; } }, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.record(); await h.app.stopRecording(); equal(!!h.view.recording.clip, true);
    await h.app.load(); equal(!!h.view.recording.clip, true);
    revision = 'r2'; await h.app.load(); equal(h.view.recording.clip, undefined); equal(v.revoked.length, 1); await h.app.dispose();
  });

  test('只持久化作业、修订和句子位置，不保存录音或播放设置', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder, bookmark);
    await h.app.mount(); h.app.select('p2'); h.app.setRate(.75);
    await h.app.record(); await h.app.stopRecording(); h.app.markBookmark();
    equal([...storage.values], [['xiyun:practice:["course","c1"]', '{"revision_id":"r1","phrase_id":"p2"}']]);
    equal(h.view.recording.clip.take, 1); await h.app.dispose();
    equal(bookmark.read(fixture), 'p2');
  });

  test('标记第二句后新页面恢复该句，不自动播放或恢复录音', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    const first = harness(fixture, origin, null, new TestPlayer(), undefined, bookmark);
    await first.app.mount(); first.app.select('p2'); first.view.actions.mark(); await first.app.dispose();
    const next = harness(fixture, origin, null, new TestPlayer(), undefined, new PracticeBookmark({ storage: () => storage }));
    await next.app.mount(); equal(next.session.current.id, 'p2'); equal(next.view.bookmark.marked, true);
    equal(next.view.bookmark.message.includes('上次'), true); equal(next.player.calls, []);
    equal(next.view.recording.clip, undefined); await next.app.dispose();
  });

  test('仅选句不写记录；切到第三句或重验课包不覆盖第二句标记', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    const h = harness(fixture, origin, null, new TestPlayer(), undefined, bookmark);
    await h.app.mount(); h.app.select('p2'); equal(storage.values.size, 0); h.app.markBookmark();
    h.app.select('p3'); await h.app.play(); equal(h.player.calls, ['p3']);
    await h.view.actions.show(); equal(h.session.current.id, 'p3'); equal(bookmark.read(fixture), 'p2');
    equal(h.view.bookmark.marked, false); await h.app.dispose();
  });

  test('清除只移除待练位置，当前句与录音保留，重开回到第一句', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    storage.setItem('other-app', 'keep');
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder, bookmark);
    await h.app.mount(); h.app.select('p2'); h.app.markBookmark();
    await h.app.record(); await h.app.stopRecording(); h.view.actions.clearBookmark();
    equal(h.session.current.id, 'p2'); equal(h.view.recording.clip.take, 1);
    equal([...storage.values], [['other-app', 'keep']]); equal(h.view.bookmark.savedId, null);
    await h.app.dispose(); const next = harness(fixture, origin, null, new TestPlayer(), undefined, bookmark);
    await next.app.mount(); equal(next.session.current.id, 'p1'); await next.app.dispose();
  });

  test('作业编号与样例模式分别隔离待练记录', () => {
    const storage = memoryStorage();
    const actual = new PracticeBookmark({ storage: () => storage });
    const demo = new PracticeBookmark({ storage: () => storage, scope: 'demo' });
    const other = { ...fixture, course_id: 'c2' };
    actual.save(fixture, 'p2'); actual.save(other, 'p3'); demo.save(fixture, 'p1');
    equal(actual.read(fixture), 'p2'); equal(actual.read(other), 'p3'); equal(demo.read(fixture), 'p1');
    demo.clear(fixture); equal(actual.read(fixture), 'p2'); equal(actual.read(other), 'p3');
  });

  test('修订改变、标记句被删除或空作业时清除旧标记', async () => {
    for (const updated of [
      { ...fixture, revision_id: 'r2' },
      { ...fixture, phrases: fixture.phrases.filter(phrase => phrase.id !== 'p2') },
      { ...fixture, phrases: [] }
    ]) {
      const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
      bookmark.save(fixture, 'p2');
      const h = harness(updated, origin, null, new TestPlayer(), undefined, bookmark);
      await h.app.mount(); equal(h.session.current?.id ?? null, updated.phrases[0]?.id ?? null);
      equal(storage.values.size, 0); equal(h.view.bookmark.savedId, null); await h.app.dispose();
    }
  });

  test('坏 JSON、空值和错误字段不会恢复或阻止听练', () => {
    for (const raw of ['broken', 'null', '3', '[]', '{}', '{"revision_id":"r1","phrase_id":"missing"}']) {
      const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
      storage.setItem('xiyun:practice:["course","c1"]', raw);
      equal(bookmark.read(fixture), null); equal(storage.values.size, 0); equal(bookmark.message, '');
    }
  });

  test('浏览器禁用存储时解释原因，试听和录音仍正常', async () => {
    const bookmark = new PracticeBookmark({ storage: () => { throw new DOMException('Blocked', 'SecurityError'); } });
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder, bookmark);
    await h.app.mount(); equal(h.session.current.id, 'p1'); equal(h.view.bookmark.message.includes('存储'), true);
    h.app.markBookmark(); equal(h.view.bookmark.savedId, null);
    await h.app.play(); equal(h.player.calls, ['p1']);
    await h.app.record(); await h.app.stopRecording(); equal(h.view.recording.clip.take, 1); await h.app.dispose();
  });

  test('写入或清除失败时保留已有标记，不假报保存或删除成功', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    bookmark.save(fixture, 'p2');
    storage.setItem = () => { throw new DOMException('Full', 'QuotaExceededError'); };
    storage.removeItem = () => { throw new DOMException('Denied', 'SecurityError'); };
    const h = harness(fixture, origin, null, new TestPlayer(), undefined, bookmark);
    await h.app.mount(); h.app.select('p3'); h.app.markBookmark();
    equal(h.view.bookmark.savedId, 'p2'); equal(h.view.bookmark.message.includes('存储'), true);
    h.app.clearBookmark(); equal(h.view.bookmark.savedId, 'p2'); equal(bookmark.read(fixture), 'p2'); await h.app.dispose();
  });

  test('标记或清除不会中断当前播放与录音', async () => {
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); await h.app.play(); const stopped = h.player.stopped;
    h.app.markBookmark(); h.app.clearBookmark(); equal(h.player.stopped, stopped); equal(h.view.playback.phase, 'playing');
    await h.app.record(); h.app.markBookmark(); h.app.clearBookmark();
    equal(v.tracks[0].stopped, false); equal(h.view.recording.phase, 'recording');
    await h.app.stopRecording(); equal(h.view.recording.clip.take, 1); await h.app.dispose();
  });

  test('离页返回恢复标记且录音已清除，无标记时回到首句', async () => {
    const v = voiceHarness(), h = harness(fixture, origin, null, new TestPlayer(), v.recorder);
    await h.app.mount(); h.app.select('p2'); h.app.markBookmark(); h.app.select('p3');
    await h.app.record(); await h.app.stopRecording(); await h.view.actions.leave();
    await h.view.actions.show(); equal(h.session.current.id, 'p2'); h.app.select('p3'); equal(h.view.recording.clip, undefined);
    h.app.clearBookmark(); await h.view.actions.leave(); await h.view.actions.show();
    equal(h.session.current.id, 'p1'); equal(v.revoked.length, 1); await h.app.dispose();
  });

  test('课包撤回或读取失败时不以本地标记替代有效课包', async () => {
    for (const code of ['COURSE_GONE', 'NETWORK']) {
      const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
      bookmark.save(fixture, 'p2');
      const h = harness(fixture, origin, { async load() { throw new CourseError(code, '暂不可用'); } }, new TestPlayer(), undefined, bookmark);
      await h.app.mount(); equal(h.session.course, null); equal(h.view.bookmark.canMark, false);
      equal(h.player.calls, []); equal(bookmark.read(fixture), 'p2'); await h.app.dispose();
    }
  });

  test('已加载作业撤回后禁用标记，仍可清除本地位置', async () => {
    let gone = false; const h = harness(fixture, origin, { async load() {
      if (gone) throw new CourseError('COURSE_GONE', '已撤回');
      return structuredClone(fixture);
    } });
    await h.app.mount(); h.app.select('p2'); h.app.markBookmark(); gone = true; await h.app.load();
    equal(h.view.bookmark.canMark, false); h.app.select('p3'); h.app.markBookmark(); equal(h.view.bookmark.savedId, 'p2');
    h.app.clearBookmark(); equal(h.view.bookmark.savedId, null); await h.app.dispose();
  });

  test('取消或过期的请求不能恢复旧标记和旧选句', async () => {
    const storage = memoryStorage(), bookmark = new PracticeBookmark({ storage: () => storage });
    bookmark.save(fixture, 'p2'); const pending = deferred(); let reads = 0;
    const h = harness(fixture, origin, { async load() { return ++reads === 1 ? pending.promise : structuredClone(fixture); } }, new TestPlayer(), undefined, bookmark);
    const old = h.app.mount(); await h.app.load(); h.app.select('p3');
    pending.resolve({ ...fixture, revision_id: 'old' }); await old;
    equal(h.session.current.id, 'p3'); equal(h.session.course.revision_id, 'r1'); equal(bookmark.read(fixture), 'p2'); await h.app.dispose();
  });

  test('实际视图分别显示当前句与待练句，按钮及清除后的状态正确', () => {
    const buttons = fixture.phrases.map(phrase => ({ dataset: { phraseId: phrase.id }, saved: { hidden: true }, querySelector() { return this.saved; } }));
    const elements = new Map();
    const element = id => {
      if (!elements.has(id)) elements.set(id, { querySelectorAll: () => buttons });
      return elements.get(id);
    };
    const view = new PracticeView({ document: { getElementById: element }, window: {}, demo: true });
    view.setBookmark({ savedId: 'p2', course: fixture, canMark: true, marked: false });
    equal(element('mark-bookmark').disabled, false); equal(element('clear-bookmark').disabled, false);
    equal(buttons.map(button => button.saved.hidden), [true, false, true]);
    equal(element('bookmark-message').textContent.includes('第 2 句'), true);
    view.setBookmark({ savedId: 'p2', course: fixture, canMark: true, marked: true });
    equal(element('mark-bookmark').disabled, true); equal(element('mark-bookmark').textContent, '已标记本句');
    view.setBookmark({ savedId: null, course: fixture, canMark: true, marked: false });
    equal(element('clear-bookmark').disabled, true); equal(buttons.every(button => button.saved.hidden), true);
    view.setBookmark({ savedId: null, course: null, canMark: false, marked: false, message: '存储不可用' });
    equal(element('mark-bookmark').disabled, true); equal(element('bookmark-message').textContent, '存储不可用');
  });

  const signal = (hz, start = 0, duration = 1800) => ({sampleRate:8000,start,
    samples:Float32Array.from({length:Math.round(duration * 8)}, (_, i) => .16 * Math.sin(2 * Math.PI * hz * i / 8000))});

  test('本机音高对听使用两条音轨各自的绝对时间', () => {
    const result = compare(signal(262, 4000), signal(294, 9000));
    equal(result.status, 'comparison'); equal(result.higher, true); equal(result.difference, 2);
    equal(result.evidence.take - result.evidence.reference, 5000);
  });

  test('本机分析在静音、等音高或片段长度悬殊时不产生纠正', () => {
    equal(compare(signal(0), signal(294)).status, 'unavailable');
    equal(compare(signal(262), signal(262)).status, 'no-difference');
    equal(compare(signal(262), signal(262, 0, 4000)).status, 'unavailable');
  });

  test('分段候选保留相对静音区间和来源偏移', () => {
    const clip = signal(262, 1300);
    clip.samples.fill(0, 3000, 7000);
    const ranges = findActivity(clip);
    equal(ranges.length, 2); equal(ranges[0].start, 1300);
    equal(ranges[0].end < ranges[1].start, true);
  });

  const results = [];
  for (const { name, run: check } of tests) {
    try { await check(); results.push({ name, passed: true }); }
    catch (error) { results.push({ name, passed: false, message: error.message }); }
  }
  return results;
}
