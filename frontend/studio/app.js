import { decodeTrack } from '../audio/media.js';
import { VoiceRecorder } from '../audio/recorder.js';
import { audioEnvelope } from '../audio/features.js';

const $ = id => document.getElementById(id);
const audio = $('backing');
const original = new Audio();
original.preload = 'none';
const replay = new Audio();
const recorder = new VoiceRecorder();
const reduceMotion = matchMedia('(prefers-reduced-motion: reduce)');
const mobile = matchMedia('(max-width:760px)');
const formatTime = ms => {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return Math.floor(seconds / 60) + ':' + String(seconds % 60).padStart(2, '0');
};
const element = (tag, text, style) => {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (style) node.className = style;
  return node;
};
const button = (label, action) => {
  const item = element('button', label, 'button secondary');
  item.type = 'button';
  item.addEventListener('click', action);
  return item;
};

async function request(url) {
  const response = await fetch(url, {cache:'no-store'});
  const body = await response.json();
  if (!response.ok) throw new Error(body.message || '暂时无法打开。');
  return body;
}

async function loadCatalog() {
  $('home').hidden = false;
  const status = $('catalog-status');
  try {
    const songs = await request('/api/karaoke/songs');
    status.hidden = !!songs.length;
    status.textContent = '本机曲库暂无曲目。';
    for (const entry of songs) {
      const row = element('li');
      const available = entry.available && entry.prepared;
      const card = element(available ? 'a' : 'div', undefined, 'song-card');
      if (available) card.href = '/sing/' + encodeURIComponent(entry.id);
      const cover = element('span', entry.title[0], 'song-cover');
      cover.setAttribute('aria-hidden', 'true');
      const text = element('span');
      text.append(element('strong', entry.title), element('small', entry.artist + ' · ' +
        (available ? formatTime(entry.duration_ms) : entry.available ? '正在准备歌词与评测数据' : '缺少本地音频')));
      card.append(cover, text);
      if (available) card.append(element('span', '→', 'song-arrow'));
      row.append(card);
      $('song-list').append(row);
    }
  } catch {
    status.textContent = '本地曲库暂时无法读取，请确认本机服务已启动。';
  }
}

let song, lineButtons = [], scoreByLine = new Map();
let selected = null, active = null, following = true, lastRendered = null;
let take = null, mode = 'idle', capture = null;
let worker = null, analysisId = 0, pending = false, analysisTimer;
let coaches = new Map();
let guideLimit = Infinity, takeLimit = Infinity, originalLimit = Infinity, frameId = null;
let evidenceTracks = [], evidenceFrame = null;

function message(text, error = false) {
  const status = $('studio-status');
  status.hidden = !text;
  status.textContent = text;
  status.dataset.error = String(error);
}

function controls() {
  $('record-label').textContent = {asking:'取消等待', recording:'结束录音', starting:'取消录音', finishing:'正在保存…'}[mode] || '开始录音';
  $('record').dataset.recording = String(mode === 'recording');
  $('record').disabled = !song || mode === 'finishing';
  $('upload').disabled = !song || mode !== 'idle';
  $('seek').disabled = $('preview').disabled = mode !== 'idle';
  $('apply-offset').disabled = mode !== 'idle' || pending;
  $('timing').hidden = !take || take.guided;
}

function setDisc() {
  const playing = !audio.paused && !audio.ended;
  $('record-disc').classList.toggle('spinning', playing);
  $('preview').textContent = playing ? 'Ⅱ' : '▶';
  $('preview').setAttribute('aria-label', playing ? '暂停伴奏' : '播放伴奏');
}

function lyricAt(ms) {
  return song.lines.findIndex(line => line.start_ms <= ms && ms < line.end_ms);
}

function centerLine(index, smooth = true) {
  const row = lineButtons[index];
  if (!row) return;
  const scroll = $('lyric-scroll');
  const top = row.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop;
  scroll.scrollTo({top:Math.max(0, top - scroll.clientHeight * .32), behavior:smooth && !reduceMotion.matches ? 'smooth' : 'auto'});
}

function syncPosition() {
  if (!song) return;
  const ms = audio.currentTime * 1000;
  $('elapsed').textContent = formatTime(ms);
  $('seek').value = String(audio.currentTime);
  if (ms >= guideLimit && !audio.paused) {
    guideLimit = Infinity;
    if (mode === 'recording') finishRecording();
    else audio.pause();
  }
  const index = lyricAt(ms);
  const next = index < 0 ? null : index;
  if (active === next) return;
  if (active !== null) lineButtons[active].removeAttribute('aria-current');
  active = next;
  if (active !== null) {
    lineButtons[active].setAttribute('aria-current', 'true');
    if (following) centerLine(active);
  }
}

function tick() {
  syncPosition();
  frameId = audio.paused ? null : requestAnimationFrame(tick);
}

function stopPlayback() {
  audio.pause();
  original.pause();
  replay.pause();
  guideLimit = takeLimit = originalLimit = Infinity;
}

async function playReference(start, end = Infinity) {
  stopPlayback();
  guideLimit = end;
  audio.currentTime = Math.max(0, start / 1000);
  try { await audio.play(); }
  catch { message('音频暂时无法播放。请检查本机服务或重试。', true); }
}

function clearResults() {
  clearTimeout(analysisTimer);
  worker?.terminate();
  worker = null;
  pending = false;
  analysisId++;
  scoreByLine = new Map();
  coaches = new Map();
  renderResults();
}

function renderResults() {
  for (const [index, item] of lineButtons.entries()) {
    const result = scoreByLine.get(song.lines[index].id);
    const label = item.querySelector('output');
    label.textContent = result?.status === 'scored' ? result.score + ' 分' : result?.status === 'unavailable' ? '待确认' : '';
    label.dataset.scored = String(result?.status === 'scored');
  }
  if (selected !== null && take) showFeedback(selected);
}

function runAnalysis(samples, sampleRate) {
  if (!take || !song) return;
  const id = ++analysisId;
  pending = true;
  controls();
  message('正在本机分析录音，完成后点歌词查看得分。');
  const finish = ({results, error}) => {
    if (id !== analysisId) return;
    clearTimeout(analysisTimer);
    pending = false;
    controls();
    if (error) { message(error, true); return; }
    scoreByLine = new Map(results.map(result => [result.id, result]));
    const count = results.filter(result => result.status === 'scored').length;
    if (take.sourceCheck) {
      message('原曲对照测试 · ' + count + ' 句可比较。个人演唱会使用人声参考评测。');
    } else {
      message(count ? '点评已生成 · 点歌词查看 ' + count + ' 句的得分与建议'
        : '这次录音没有足够可比较的人声音高。点歌词查看原因。');
    }
    renderResults();
  };
  try {
    worker ??= new Worker(new URL('./scoring-worker.js', import.meta.url), {type:'module'});
    worker.onmessage = ({data}) => { if (data.id === id) finish(data); };
    worker.onerror = () => finish({error:'本机分析没有完成。录音仍可回听。'});
    const lines = song.lines.map(line => ({
      id:line.id, start_ms:line.start_ms, end_ms:line.end_ms,
      reference:take.sourceCheck ? line.mix_reference ?? line.reference : line.reference,
      model_reference:line.model_reference, words:line.words
    }));
    const job = {id, lines, modelReady:!take.sourceCheck && song.pitch_engine === 'crepe-tiny',
      timing:{origin:take.origin,guided:take.guided}, samples, sampleRate};
    worker.postMessage(job, samples ? [samples.buffer] : []);
  } catch {
    finish({error:'当前浏览器无法启动评测。录音仍可回听。'});
  }
  if (pending) {
    clearTimeout(analysisTimer);
    analysisTimer = setTimeout(() => {
      worker?.terminate();
      worker = null;
      finish({error:'分析超时。可重新上传录音，或录一小段再试。'});
    }, 35000);
  }
}

async function acceptTake(blob, timing) {
  try {
    const track = await decodeTrack(blob, timing.guided);
    clearResults();
    const old = take;
    replay.pause();
    replay.removeAttribute('src');
    replay.load();
    if (old) URL.revokeObjectURL(old.url);
    const {buffer} = track;
    const channels = Array.from({length:buffer.numberOfChannels}, (_, index) => buffer.getChannelData(index));
    const samples = channels[0].slice();
    if (channels.length > 1) {
      for (const channel of channels.slice(1)) {
        for (let i = 0; i < samples.length; i++) samples[i] += channel[i];
      }
      for (let i = 0; i < samples.length; i++) samples[i] /= channels.length;
    }
    take = {...timing, url:URL.createObjectURL(blob), duration:track.duration,
      sourceCheck:!timing.guided && track.fingerprint === song.source_sha256 && song.reference_source === 'vocal',
      envelope:audioEnvelope(samples, buffer.sampleRate)};
    replay.src = take.url;
    $('offset').value = take.origin / 1000;
    mode = 'idle';
    runAnalysis(samples, buffer.sampleRate);
    renderResults();
  } catch (error) {
    mode = 'idle';
    controls();
    message(error.message || '没有读到录音。可重新录制或上传。', true);
  }
}

function abortCapture(reason, error = false) {
  capture = null;
  recorder.clear();
  stopPlayback();
  mode = 'idle';
  controls();
  message(reason, error);
}

async function beginRecording({start, end = Infinity}) {
  if (!song || mode !== 'idle') return;
  stopPlayback();
  closeFeedback(false);
  following = true;
  $('follow').hidden = true;
  audio.currentTime = start / 1000;
  const session = {startedAt:null, origin:start, recording:false};
  capture = session;
  mode = 'asking';
  controls();
  message('正在打开麦克风…');
  try {
    await recorder.start({
      onTick: () => {
        if (capture === session && session.startedAt === null) session.startedAt = performance.now();
      },
      onComplete: blob => {
        if (capture !== session) return;
        capture = null;
        void acceptTake(blob, {origin:session.origin, guided:true});
        recorder.clear();
      },
      onError: () => { if (capture === session) abortCapture('录音中断，请重试或上传已有录音。', true); },
      onInterrupted: () => { if (capture === session) abortCapture('麦克风已经断开。之前的录音仍可查看。', true); }
    });
    if (capture !== session) return;
    mode = 'starting';
    controls();
    guideLimit = end;
    await audio.play();
    if (capture !== session) { audio.pause(); return; }
    if (!session.recording) markRecording();
  } catch (error) {
    if (capture === session) abortCapture(error.name === 'NotAllowedError'
      ? '麦克风或伴奏播放权限不可用。可重试，或上传已有录音。'
      : '无法开始跟唱。请检查麦克风与本机音频。', true);
  }
}

function markRecording() {
  if (mode !== 'starting' || !capture) return;
  capture.origin = Math.round(audio.currentTime * 1000 - (performance.now() - capture.startedAt));
  capture.recording = true;
  mode = 'recording';
  controls();
  message('正在录音 · 跟着高亮歌词唱');
}

function finishRecording() {
  if (mode !== 'recording') return;
  mode = 'finishing';
  stopPlayback();
  controls();
  message('正在读取录音…');
  void recorder.stop();
}

async function playTake(start, end) {
  if (!take || mode !== 'idle') return;
  stopPlayback();
  takeLimit = Math.min(end, take.duration);
  replay.currentTime = Math.max(0, start / 1000);
  try { await replay.play(); }
  catch { message('这段录音暂时无法回听。', true); }
}

async function playOriginal(start, end) {
  if (mode !== 'idle') return;
  stopPlayback();
  originalLimit = end;
  original.currentTime = start / 1000;
  try { await original.play(); }
  catch { message('这段原唱暂时无法回听。', true); }
}

function syncEvidenceTracks() {
  let playing = false;
  for (const track of evidenceTracks) {
    const elapsed = track.player.currentTime * 1000 - track.start;
    const progress = Math.min(1, Math.max(0, elapsed / (track.end - track.start)));
    if (track.engaged && !track.player.paused && progress === 1) track.player.pause();
    const active = track.engaged && !track.player.paused && !track.player.ended;
    if (track.row.dataset.playing !== String(active)) {
      track.row.dataset.playing = String(active);
      track.toggle.textContent = active ? 'Ⅱ' : '▶';
      track.toggle.setAttribute('aria-label', (active ? '暂停' : '播放') + track.label);
    }
    track.wave.style.setProperty('--played', (track.engaged ? progress * 100 : 0) + '%');
    playing ||= active;
  }
  if (playing && evidenceFrame === null) evidenceFrame = requestAnimationFrame(() => {
    evidenceFrame = null;
    syncEvidenceTracks();
  });
}

function evidenceTrack({kind, label, start, end, marker, levels, play, player}) {
  const row = element('div', undefined, 'evidence-track');
  row.dataset.kind = kind;
  const toggle = element('button', '▶', 'evidence-toggle');
  toggle.type = 'button';
  toggle.setAttribute('aria-label', '播放' + label);
  const info = element('div', undefined, 'evidence-track-info');
  info.append(element('strong', label), element('span', ((end - start) / 1000).toFixed(1) + ' 秒'));
  const wave = element('div', undefined, 'evidence-wave');
  wave.setAttribute('aria-hidden', 'true');
  const heights = levels.filter(([ms]) => ms >= start - 90 && ms < end).map(([, value]) => value);
  const peak = heights.reduce((max, value) => Math.max(max, value), .015);
  const bars = element('div', undefined, 'evidence-bars');
  const progress = element('div', undefined, 'evidence-bars evidence-played');
  for (let index = 0; index < 42; index++) {
    const height = heights[Math.floor(index * heights.length / 42)] || 0;
    const bar = element('i');
    bar.style.setProperty('--level', Math.round(10 + Math.pow(Math.min(1, height / peak), .65) * 82) + '%');
    bars.append(bar);
    progress.append(bar.cloneNode());
  }
  wave.append(bars, progress);
  if (Number.isFinite(marker) && start <= marker && marker < end) {
    const pin = element('i', undefined, 'evidence-marker');
    pin.style.left = ((marker - start) / (end - start) * 100) + '%';
    wave.append(pin);
  }
  const track = {row, wave, toggle, player, label, start, end, engaged:false};
  toggle.addEventListener('click', async () => {
    if (track.engaged && !player.paused) { player.pause(); return; }
    if (track.engaged && player.currentTime * 1000 > start && player.currentTime * 1000 < end - 60) {
      await play(player.currentTime * 1000, end);
    } else {
      track.engaged = true;
      await play(start, end);
    }
    syncEvidenceTracks();
  });
  evidenceTracks.push(track);
  row.append(toggle, info, wave);
  return row;
}

function renderEvidence(line, result) {
  evidenceTracks = [];
  const section = element('section', undefined, 'evidence');
  const heading = element('div', undefined, 'evidence-heading');
  heading.append(element('h4', '对照回听'));
  if (result?.reference_ms !== undefined) heading.append(element('span', '竖线是点评位置'));
  section.append(heading);
  const offset = result?.offset ?? take.origin;
  section.append(evidenceTrack({kind:'original',label:'原唱',start:line.start_ms,end:line.end_ms,
    marker:result?.reference_ms, levels:line.waveform ?? line.reference.map(([ms,, db]) => [ms, 10 ** (db / 20)]),
    play:playOriginal,player:original}));
  const start = Math.max(0, line.start_ms - offset);
  const end = Math.min(take.duration, line.start_ms - offset + (line.end_ms - line.start_ms) * (result?.scale ?? 1));
  if (end > start) {
    section.append(evidenceTrack({kind:'take',label:'我的录音',start,end,marker:result?.take_ms,
      levels:take.envelope,play:playTake,player:replay}));
  } else {
    section.append(element('p', '这次录音没有覆盖该句。', 'evidence-empty'));
  }
  return section;
}

function coachPanel(line, result, basic) {
  const box = element('section', undefined, 'feedback-advice ai-coach');
  const title = element('h4', 'AI 声乐点评');
  const body = element('div');
  box.append(title, body);
  function render(entry) {
    body.replaceChildren();
    if (entry.state === 'ready') {
      const {score, assessment, exercise, expression, diction} = entry.value;
      if (basic) basic.hidden = true;
      body.append(element('p', 'AI 练习分 ' + score + ' · ' + assessment), element('p', exercise));
      if (expression || diction) {
        const more = element('details', undefined, 'coach-note');
        more.append(element('summary', '表演与咬字练法'));
        if (expression) more.append(element('p', expression));
        if (diction) more.append(element('p', diction));
        body.append(more);
      }
    } else if (entry.state === 'loading') {
      const status = element('p', '正在生成本句练法…', 'coach-note');
      status.setAttribute('role', 'status');
      body.append(status);
    } else {
      body.append(element('p', '本机 AI 暂未生成点评，可以先按基础练法对照。', 'coach-note'),
        button('重新生成', () => void load()));
    }
  }
  function load() {
    const existing = coaches.get(line.id);
    if (existing?.state === 'loading') return;
    const entry = {state:'loading'};
    coaches.set(line.id, entry);
    render(entry);
    entry.pending = fetch('/api/karaoke/coach', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body:JSON.stringify({song_id:song.id, line_id:line.id, facts:result.facts}),
        signal:AbortSignal.timeout(28000)}).then(async response => {
      if (!response.ok) throw new Error('coach');
      entry.value = await response.json();
      entry.state = 'ready';
    }).catch(() => { entry.state = 'error'; });
    void entry.pending.then(() => { if (box.isConnected) render(entry); });
  }
  const existing = coaches.get(line.id);
  if (existing) {
    render(existing);
    if (existing.state === 'loading') void existing.pending.then(() => { if (box.isConnected) render(existing); });
  } else load();
  return box;
}

function closeFeedback(restoreFocus = true) {
  const focused = selected;
  if (selected !== null) lineButtons[selected].setAttribute('aria-pressed', 'false');
  selected = lastRendered = null;
  evidenceTracks = [];
  stopPlayback();
  $('deck').classList.remove('leaving');
  $('deck').removeAttribute('aria-hidden');
  $('feedback').hidden = true;
  if (restoreFocus && focused !== null) lineButtons[focused].focus();
}

function showFeedback(index, focus = false) {
  if (!take || mode !== 'idle') return;
  if (selected !== null && selected !== index) lineButtons[selected].setAttribute('aria-pressed', 'false');
  selected = index;
  const line = song.lines[index], result = scoreByLine.get(line.id);
  lineButtons[index].setAttribute('aria-pressed', 'true');
  if (lastRendered !== index) {
    $('feedback-line').textContent = line.text;
    lastRendered = index;
  }
  const content = $('feedback-content');
  content.replaceChildren();
  const outcome = element('div', undefined, 'feedback-outcome');
  if (result?.status === 'scored') {
    const score = element('div', undefined, 'score');
    const category = take.sourceCheck ? '同源对照分' : result.coverage < 35 ? '片段音高分' : '音高分';
    score.append(element('strong', result.score), element('span', category));
    outcome.append(score, element('p', take.sourceCheck ? '同一文件，用于检查时间窗和回放。' : result.description));
  } else {
    outcome.append(element('p', result?.description || (pending ? '正在分析这一句…' : '可以先对照听两段。')));
  }
  content.append(outcome, renderEvidence(line, result));
  let basic;
  if (result?.advice && !take.sourceCheck) {
    basic = element('section', undefined, 'feedback-advice');
    basic.append(element('h4', '这样练'), element('p', result.advice));
    content.append(basic);
  }
  if (!take.sourceCheck && result?.facts && result.facts.coverage >= 20) content.append(coachPanel(line, result, basic));
  const footer = element('div', undefined, 'feedback-footer');
  footer.append(button('再唱这一句', () => void beginRecording({start:Math.max(0,line.start_ms - 1400), end:line.end_ms})));
  if (result?.status === 'scored') {
    const detail = element('details');
    const method = result.engine === 'crepe-tiny' ? 'CREPE 音高模型' : '本机声学算法';
    detail.append(element('summary', '评分依据'), element('p', method + ' · 本句参考音高里有 ' + result.coverage + '% 能与录音比较。'));
    footer.append(detail);
  }
  content.append(footer);
  $('deck').classList.add('leaving');
  $('deck').setAttribute('aria-hidden', 'true');
  $('feedback').hidden = false;
  if (focus) {
    $('feedback-line').focus({preventScroll:true});
    if (mobile.matches) $('feedback').scrollIntoView({block:'nearest',behavior:reduceMotion.matches ? 'auto' : 'smooth'});
  }
}

function showLines() {
  const list = $('lyrics');
  lineButtons = song.lines.map((line, index) => {
    const item = element('li', undefined, 'lyric');
    const row = element('button');
    row.type = 'button';
    row.setAttribute('aria-pressed', 'false');
    row.append(element('time', formatTime(line.start_ms)), element('span', line.text, 'lyric-text'), element('output', ''));
    row.addEventListener('click', () => {
      if (mode !== 'idle') return;
      if (take) {
        following = false;
        $('follow').hidden = false;
        stopPlayback();
        showFeedback(index, true);
      } else {
        following = true;
        void playReference(line.start_ms);
      }
    });
    item.append(row);
    return item.firstChild;
  });
  list.replaceChildren(...lineButtons.map(row => row.parentElement));
}

async function loadStudio(id) {
  $('studio').hidden = $('back-home').hidden = false;
  try {
    song = await request('/api/karaoke/songs/' + encodeURIComponent(id));
    if (!song.lines.length) throw new Error('这首曲目还没有时间轴。');
    for (const key of ['song-title','disc-title','deck-title']) $(key).textContent = song.title;
    $('artist').textContent = $('deck-artist').textContent = song.artist;
    $('duration').textContent = formatTime(song.duration_ms);
    $('backing-note').hidden = !song.backing_is_mix;
    $('seek').max = String(song.duration_ms / 1000);
    audio.src = song.backing_url;
    original.src = song.reference_url;
    showLines();
    $('practice').hidden = false;
    controls();
    document.title = song.title + ' · 戏韵 AI 学伴';
  } catch (error) {
    $('song-title').textContent = '暂时无法打开曲目';
    message(error.message || '本机曲目暂不可用。', true);
  }
}

$('record').addEventListener('click', () => {
  if (mode === 'recording') finishRecording();
  else if (mode === 'asking' || mode === 'starting') abortCapture('已取消录音。');
  else if (mode === 'idle') void beginRecording({start:Math.round(audio.currentTime * 1000) >= song.duration_ms - 250
    ? 0 : Math.round(audio.currentTime * 1000)});
});
$('preview').addEventListener('click', () => {
  if (audio.paused) {
    following = true;
    $('follow').hidden = true;
    void playReference(audio.ended ? 0 : audio.currentTime * 1000);
  } else audio.pause();
});
$('seek').addEventListener('input', () => {
  following = true;
  $('follow').hidden = true;
  guideLimit = Infinity;
  original.pause();
  replay.pause();
  audio.currentTime = Number($('seek').value);
  syncPosition();
});
$('follow').addEventListener('click', () => {
  following = true;
  $('follow').hidden = true;
  if (active !== null) centerLine(active);
});
$('lyric-scroll').addEventListener('wheel', () => {
  following = false;
  $('follow').hidden = false;
}, {passive:true});
$('lyric-scroll').addEventListener('touchmove', () => {
  following = false;
  $('follow').hidden = false;
}, {passive:true});
$('close-feedback').addEventListener('click', () => closeFeedback());
$('upload').addEventListener('click', () => $('file').click());
$('file').addEventListener('change', () => {
  const file = $('file').files[0];
  $('file').value = '';
  if (!file || mode !== 'idle') return;
  stopPlayback();
  mode = 'finishing';
  controls();
  message('正在本机打开录音…');
  void acceptTake(file, {origin:0, guided:false});
});
$('apply-offset').addEventListener('click', () => {
  if (!take || !worker || !$('offset').value) return;
  const origin = Math.round(Number($('offset').value) * 1000);
  if (!Number.isFinite(origin) || origin < -60000 || origin >= song.duration_ms) {
    message('请输入原曲范围内的起点，最多可提前 60 秒。', true);
    return;
  }
  take.origin = origin;
  coaches = new Map();
  scoreByLine.clear();
  runAnalysis();
  renderResults();
});
audio.addEventListener('playing', () => {
  markRecording();
  setDisc();
  if (frameId === null) tick();
});
audio.addEventListener('pause', () => { setDisc(); syncPosition(); });
audio.addEventListener('seeked', syncPosition);
audio.addEventListener('ended', () => {
  setDisc();
  if (mode === 'recording') finishRecording();
});
audio.addEventListener('error', () => {
  if (capture) abortCapture('伴奏播放中断，之前的录音仍可查看。', true);
});
replay.addEventListener('timeupdate', () => {
  if (replay.currentTime * 1000 >= takeLimit) replay.pause();
});
original.addEventListener('timeupdate', () => {
  if (original.currentTime * 1000 >= originalLimit) original.pause();
});
for (const player of [replay, original]) {
  for (const event of ['playing', 'pause', 'ended']) player.addEventListener(event, syncEvidenceTracks);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden && capture) abortCapture('页面已切到后台，录音已取消。');
});
window.addEventListener('pagehide', event => {
  if (capture) abortCapture('离开唱段页后已取消本次录音。');
  stopPlayback();
  if (event.persisted) return;
  recorder.clear();
  clearTimeout(analysisTimer);
  worker?.terminate();
  if (take) URL.revokeObjectURL(take.url);
});

const route = location.pathname.match(/^\/sing\/([a-z0-9-]+)$/);
if (route) void loadStudio(route[1]);
else void loadCatalog();
