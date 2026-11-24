import { analysisClip, canPreservePitch, CAPTURE_LIMIT, decodeTrack, openExample, player, recorder } from './media.js';
import { eraseProject, getProject, getProjects, saveProject, trackRecord } from './storage.js';
import { explainLine } from './knowledge.js';
import { REPORT_LIMIT_MS } from './analysis.js';

const $ = id => document.getElementById(id);
const roles = {
  reference: {label:'参考原音', import:'导入原音', play:'听示范'},
  take: {label:'我的演唱', import:'导入我的演唱', play:'听自己'}
};
const exampleRanges = [[0, 3900], [4000, 8000], [8100, 12000]];
const seconds = ms => (ms / 1000).toFixed(3).replace(/(\.\d{2})0$/, '$1');
const newProject = () => ({id:crypto.randomUUID(), version:null});
let project = newProject();
let epoch = 0;
let storageBusy = false;
let capture = null;
let explainedMic = false;
let playback = null;
let worker = null;
let loaded = {};
let previousTake = null;
let viewingPrevious = false;
let segments = [];

const visibleTrack = role => role === 'take' && viewingPrevious ? previousTake : loaded[role];
const phraseKey = (track = loaded.reference) => track ? [track.id, track.start, track.end].join('/') : null;

function node(tag, text = '', className = '') {
  const item = document.createElement(tag);
  item.textContent = text;
  if (className) item.className = className;
  return item;
}

function button(label, action, className = 'button') {
  const item = node('button', label, className);
  item.type = 'button';
  item.addEventListener('click', action);
  return item;
}

function notice(element, text, error = false) {
  if (element.textContent !== text) element.textContent = text;
  element.hidden = !text;
  element.dataset.error = String(error);
}

const tracks = Object.fromEntries(Object.entries(roles).map(([role, config]) => {
  const element = $('track-template').content.firstElementChild.cloneNode(true);
  element.dataset.role = role;
  $('tracks').append(element);
  const parts = Object.fromEntries([...element.querySelectorAll('[data-part]')].map(part => [part.dataset.part, part]));
  parts.title.textContent = config.label;
  parts.import.textContent = config.import;
  parts.play.textContent = config.play;
  parts.file.setAttribute('aria-label', config.import);
  parts.wave.setAttribute('aria-label', config.label + '的波形。可以拖动选段，也可以输入起止时间。');
  for (const name of ['start','end']) parts[name].setAttribute('aria-label', config.label + (name === 'start' ? '起点（秒）' : '终点（秒）'));
  return [role, {parts, sequence:0, busy:false, drag:null}];
}));

function drawTrack(role, playbackMs = null, selection = null) {
  const track = visibleTrack(role);
  if (!track) return;
  const canvas = tracks[role].parts.wave;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;
  const {width:w, height:h} = canvas;
  const scale = w / track.duration;
  const [start, end] = selection || [track.start, track.end];
  ctx.fillStyle = '#142c22';
  ctx.fillRect(0, 0, w, h);
  ctx.fillStyle = role === 'reference' ? '#4e4731' : '#264b3b';
  ctx.fillRect(start * scale, 0, (end - start) * scale, h);
  ctx.strokeStyle = '#4f6859';
  ctx.beginPath();
  for (let i = 1; i < 8; i++) { ctx.moveTo(w * i / 8, 0); ctx.lineTo(w * i / 8, h); }
  ctx.moveTo(0, h / 2); ctx.lineTo(w, h / 2); ctx.stroke();
  const dx = w / track.peaks.length;
  track.peaks.forEach((peak, i) => {
    const x = (i + .5) * dx;
    const inside = x >= start * scale && x < end * scale;
    ctx.fillStyle = inside ? (role === 'reference' ? '#ebc893' : '#b8dbc3') : '#81988b';
    const size = Math.max(2, Math.sqrt(peak) * (h - 12));
    ctx.fillRect(x - dx * .29, (h - size) / 2, dx * .58, size);
  });
  ctx.fillStyle = '#f0d5a4';
  ctx.fillRect(Math.min(w - 2, start * scale), 0, 2, h);
  ctx.fillRect(Math.min(w - 2, end * scale), 0, 2, h);
  if (playbackMs !== null) {
    ctx.fillStyle = '#fff9ea';
    ctx.fillRect(Math.min(w - 3, playbackMs * scale), 0, 3, h);
  }
}

function renderTrack(role) {
  const track = visibleTrack(role), pending = project[role];
  const parts = tracks[role].parts;
  parts.name.textContent = track?.name || (pending ? '重新选择：' + pending.name : role === 'reference'
    ? '选择你想学的唱段' : loaded.reference ? '导入已有演唱，或现场录一遍' : '导入原唱后，可加入自己的演唱');
  parts.name.classList.add('track-name');
  for (const id of ['visual','range','duration','play']) parts[id].hidden = !track;
  parts.remove.hidden = !track && !pending;
  parts.import.textContent = track ? '更换音频' : roles[role].import;
  parts.file.disabled = parts.import.disabled = storageBusy || tracks[role].busy || !!capture;
  parts.remove.disabled = storageBusy || tracks[role].busy || !!capture;
  parts.play.disabled = !!capture;
  if (role === 'take') {
    parts.advice.hidden = !loaded.reference || !loaded.take || viewingPrevious || !!capture;
    parts.play.textContent = viewingPrevious ? '听上一遍' : roles.take.play;
    parts.range.hidden = !track || viewingPrevious;
    parts.remove.hidden = viewingPrevious || (!track && !pending);
    parts.attempts.hidden = !previousTake;
    const samePhrase = previousTake?.phrase && previousTake.phrase === phraseKey()
      && previousTake.phrase === loaded.take?.phrase;
    parts['attempt-label'].textContent = samePhrase ? '比较两遍' : '上一遍录音 · 单独回听';
    parts.previous.textContent = samePhrase ? '刚才' : '上一遍';
    for (const [part, active] of [[parts.current, !viewingPrevious], [parts.previous, viewingPrevious]]) {
      part.setAttribute('aria-pressed', String(active));
      part.disabled = storageBusy || tracks.take.busy || !!capture;
    }
    parts['remove-previous'].disabled = parts.previous.disabled;
    parts.record.hidden = !!capture;
    parts.record.textContent = loaded.take ? '再唱一次' : '录一遍';
    parts.record.disabled = storageBusy || tracks[role].busy || tracks.reference.busy;
    parts.capture.hidden = !capture;
    parts.capture.disabled = capture?.state === 'finishing';
    parts.capture.textContent = ({requesting:'取消等待',finishing:'正在结束…'})[capture?.state] || '结束录音';
    parts.cancel.hidden = capture?.state !== 'recording';
  }
  if (!track) return;
  parts.duration.textContent = seconds(track.duration) + ' 秒';
  parts.start.value = seconds(track.start);
  parts.end.value = seconds(track.end);
  parts.start.max = parts.end.max = seconds(track.duration);
  parts.extent.textContent = seconds(track.duration) + ' 秒';
  parts.time.textContent = '';
  parts['range-title'].textContent = '选段 ' + seconds(track.start) + '–' + seconds(track.end) + ' 秒';
  drawTrack(role);
}

function controls() {
  const reference = loaded.reference;
  const busy = Object.values(tracks).some(slot => slot.busy);
  $('current-phrase').hidden = !reference;
  if (reference) {
    const transcript = $('transcript').value.trim();
    const singleLine = transcript && !/[\n\r]/.test(transcript);
    $('phrase-text').textContent = singleLine ? transcript : seconds(reference.start) + '–' + seconds(reference.end) + ' 秒';
    $('phrase-time').textContent = singleLine ? seconds(reference.start) + '–' + seconds(reference.end) + ' 秒 · 当前选段' : '当前选段';
  }
  $('play-lead').hidden = !reference || reference.start === 0;
  $('play-lead').disabled = !!capture;
  $('analysis-panel').hidden = !reference || !loaded.take || viewingPrevious || !!capture;
  $('analyze').disabled = $('analyze-single').disabled = $('analysis-panel').hidden || !!worker || busy || storageBusy;
  tracks.take.parts.advice.disabled = !!capture || busy || storageBusy;
  if (!reference) tracks.take.parts.file.disabled = tracks.take.parts.import.disabled = true;
  $('rate').disabled = $('repeats').disabled = !!capture;
  $('keep-previous-field').hidden = $('save-mode').value !== 'media' || !previousTake;
  tracks.take.parts.record.disabled = !reference || storageBusy || busy;
  $('cancel-analysis').hidden = !worker || worker.type === 'segments';
  $('stop-play').disabled = !playback;
  const hasMedia = reference || loaded.take || previousTake || project.reference || project.take;
  $('save').disabled = storageBusy || !hasMedia || !!capture || busy;
  $('new-project').hidden = !hasMedia;
  $('new-project').disabled = storageBusy;
  $('open-example').disabled = storageBusy || !!capture || tracks.reference.busy;
  $('hero-import').disabled = tracks.reference.parts.import.disabled;
  document.querySelectorAll('#project-list button').forEach(control => { control.disabled = storageBusy || !!capture; });
}

function stopPlayback() {
  const old = playback;
  playback = null;
  player.stop();
  if (old) {
    tracks[old.role].parts.time.textContent = '';
    drawTrack(old.role);
  }
  notice($('play-status'), '');
  controls();
}

function updateRateNote() {
  const rate = Number($('rate').value);
  notice($('rate-note'), rate === 1 ? '' : canPreservePitch
    ? '保调慢放试用 · 适用于一分钟内的选段。'
    : '当前浏览器慢放时会降低音调。');
}

async function play(role, range, repeats = Number($('repeats').value), label = tracks[role].parts.play.textContent) {
  const track = visibleTrack(role);
  if (!track || capture) return;
  stopPlayback();
  const {start, end} = range || track;
  const rate = Number($('rate').value);
  const session = {role};
  playback = session;
  const onError = error => {
    if (playback !== session) return;
    stopPlayback();
    notice($('play-status'), error.message || '无法试听这一段。', true);
  };
  const run = async round => {
    if (playback !== session) return;
    try {
      notice($('play-status'), label + ' · 第 ' + round + ' / ' + repeats + ' 遍');
      await player.play(track, {lead_ms:start,end_ms:end}, {
        rate,
        onError,
        onFallback: () => notice($('rate-note'), '这段以降调慢放播放，可切回原速。'),
        onProgress: ({positionMs}) => {
          if (playback !== session) return;
          const time = seconds(positionMs) + ' 秒';
          const label = tracks[role].parts.time;
          if (label.textContent !== time) label.textContent = time;
          drawTrack(role, positionMs);
        },
        onEnded: () => {
          if (playback !== session) return;
          if (round < repeats) void run(round + 1);
          else stopPlayback();
        }
      });
    } catch (error) { onError(error); }
  };
  updateRateNote();
  controls();
  await run(1);
}

function evidencePlayback(role, ms, range) {
  const start = Math.max(range.start, ms - 220);
  const end = Math.min(range.end, ms + 380);
  if (end > start) void play(role, {start,end}, 1);
}

function cancelAnalysis(message = '') {
  if (worker) {
    clearTimeout(worker.timeout);
    worker.instance.terminate();
  }
  worker = null;
  notice($('analysis-status'), message);
  drawSegments();
  controls();
}

function invalidateAnalysis() {
  cancelAnalysis();
  $('result').replaceChildren();
  $('result').hidden = true;
  controls();
}

function selectRange(role, start, end) {
  const track = loaded[role];
  if (!track || capture || storageBusy || (role === 'take' && viewingPrevious)) return;
  const parts = tracks[role].parts;
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end > track.duration || end - start < 100) {
    notice(parts.status, '请选择音频范围内至少 0.1 秒的片段。', true);
    return;
  }
  if (track.start === start && track.end === end) return;
  stopPlayback();
  invalidateAnalysis();
  Object.assign(track, {start,end});
  if (role === 'reference') {
    viewingPrevious = false;
    renderTrack('take');
  } else track.phrase = null;
  renderTrack(role);
  controls();
  notice(parts.status, '已选 ' + seconds(start) + '–' + seconds(end) + ' 秒');
}

function locateInWave(role, canvas, event) {
  const box = canvas.getBoundingClientRect();
  const ratio = Math.min(1, Math.max(0, (event.clientX - box.left) / box.width));
  return Math.round(ratio * loaded[role].duration);
}

function syncProjectLabel() {
  const name = $('project-name').value.trim();
  const label = name || loaded.reference?.name || loaded.take?.name || '';
  $('current-project').textContent = label;
  $('current-project').hidden = !label;
}

function setTrack(role, track, pending = project[role]) {
  stopPlayback();
  invalidateAnalysis();
  if (pending?.fingerprint === track.fingerprint && pending.start >= 0 && pending.end <= track.duration && pending.end - pending.start >= 100) {
    track.start = pending.start;
    track.end = pending.end;
    track.id = pending.id;
    track.phrase = pending.phrase ?? null;
  }
  loaded[role] = track;
  project[role] = null;
  if (role === 'reference') {
    viewingPrevious = false;
    renderTrack('take');
    segments = [];
    drawSegments();
  } else viewingPrevious = false;
  syncProjectLabel();
  renderTrack(role);
  controls();
}

async function importTrack(role, file) {
  if (!file || storageBusy || capture || (role === 'take' && !loaded.reference)) return;
  const slot = tracks[role], sequence = ++slot.sequence;
  slot.busy = true;
  renderTrack(role);
  controls();
  notice(slot.parts.status, '正在设备上读取音频…');
  try {
    const track = await decodeTrack(file, file.name);
    if (slot.sequence !== sequence) return;
    setTrack(role, track);
    notice(slot.parts.status, '音频已就绪。可拖动波形选段，或输入时间。');
  } catch (error) {
    if (slot.sequence === sequence) notice(slot.parts.status, error.message, true);
  } finally {
    if (slot.sequence === sequence) {
      slot.busy = false;
      renderTrack(role);
      controls();
    }
  }
}

async function loadExample() {
  if (storageBusy || capture) return;
  const slot = tracks.reference, sequence = ++slot.sequence;
  slot.busy = true;
  renderTrack('reference');
  controls();
  try {
    const track = await openExample();
    if (sequence !== slot.sequence) return;
    setTrack('reference', track);
    notice(slot.parts.status, '这是用来体验播放的合成音。请导入真实唱段进行练唱。');
    $('practice-title').focus();
    await play('reference', null, 1);
  } catch (error) {
    if (sequence === slot.sequence) notice(slot.parts.status, error.message, true);
  } finally {
    if (sequence === slot.sequence) {
      slot.busy = false;
      renderTrack('reference');
      controls();
    }
  }
}

function updateRecording(text) {
  for (const role of Object.keys(tracks)) renderTrack(role);
  tracks.take.parts.status.setAttribute('role', 'status');
  notice(tracks.take.parts.status, text);
  controls();
}

function cancelCapture(message) {
  const wasCapturing = !!capture;
  capture = null;
  recorder.clear();
  updateRecording(message);
  if (wasCapturing && !document.hidden) tracks.take.parts.record.focus();
}

async function beginCapture() {
  if (!loaded.reference || capture || storageBusy || Object.values(tracks).some(slot => slot.busy)) return;
  stopPlayback();
  cancelAnalysis();
  const session = {state:'requesting', phrase:phraseKey()};
  capture = session;
  updateRecording('等待麦克风授权…');
  tracks.take.parts.capture.focus();
  try {
    await recorder.start('personal', {
      onTick: elapsed => {
        if (capture !== session) return;
        if (session.state === 'requesting') {
          session.state = 'recording';
          tracks.take.parts.capture.textContent = '结束录音';
          tracks.take.parts.cancel.hidden = false;
        }
        if (elapsed >= CAPTURE_LIMIT) void finishCapture();
        else {
          const text = '录音中 · ' + Math.floor(elapsed / 1000) + ' 秒';
          const status = tracks.take.parts.status;
          status.removeAttribute('role');
          notice(status, text);
        }
      },
      onComplete: clip => { void addCapture(session, clip); },
      onInterrupted: () => {
        if (capture === session) cancelCapture('麦克风已断开。已有录音仍可回听。');
      },
      onError: () => {
        if (capture === session) cancelCapture('录音中断。可以重录，或导入已有的演唱。');
      }
    });
  } catch (error) {
    if (capture === session) cancelCapture(error.name === 'NotAllowedError'
      ? '没有麦克风权限。仍可导入自己的演唱。'
      : '麦克风暂不可用。请重试或导入自己的演唱。');
  }
}

async function finishCapture() {
  if (capture?.state === 'requesting') { cancelCapture('已取消等待。'); return; }
  if (capture?.state !== 'recording') return;
  capture.state = 'finishing';
  tracks.take.parts.status.setAttribute('role', 'status');
  updateRecording('正在结束录音…');
  await recorder.stop();
}

async function addCapture(session, clip) {
  if (capture !== session) return;
  session.state = 'finishing';
  tracks.take.parts.status.setAttribute('role', 'status');
  updateRecording('正在读取这一遍…');
  try {
    const track = await decodeTrack(clip.blob, '本次录音', 'recording');
    if (capture !== session) return;
    if (loaded.take) {
      previousTake = loaded.take;
      $('keep-previous').checked = false;
    }
    track.phrase = session.phrase;
    setTrack('take', track, null);
    cancelCapture('录音完成。可以听自己，或生成逐句点评。');
    tracks.take.parts.play.focus();
  } catch {
    if (capture === session) cancelCapture('录音无法解码，原有演唱已保留。可重录或导入文件。');
  }
}

function requestCapture() {
  if (!loaded.reference || capture || storageBusy || Object.values(tracks).some(slot => slot.busy)) return;
  if (explainedMic) { void beginCapture(); return; }
  const dialog = $('record-consent');
  dialog.returnValue = 'cancel';
  dialog.showModal();
}

function showReport(report) {
  const panel = $('result');
  panel.replaceChildren();
  const lyrics = $('transcript').value.split(/\r?\n/).map(line => line.trim()).filter(Boolean);
  const useLyrics = !report.reason && lyrics.length === report.rows.length;
  if (report.reason) panel.append(node('p', report.reason, 'report-note'));
  if (lyrics.length && report.rows.length && !useLyrics && !report.reason) {
    panel.append(node('p', '唱词行数和分出的句数不同。请按点评顺序每句一行填写，再重新生成。', 'report-note'));
  }
  report.rows.forEach(({reference, take, feedback}, index) => {
    const line = useLyrics ? lyrics[index] : '';
    const item = node('article', '', 'phrase-review');
    const state = feedback?.status === 'comparison' ? 'improve' : feedback?.match ? 'match' : 'pending';
    const heads = node('div', '', 'phrase-review-heading');
    const title = line || '第 ' + (index + 1) + ' 句';
    const label = {improve:'这一处可改进', match:'有一处音高贴近原唱', pending:'暂不能判断'}[state];
    const badge = node('span', label, 'phrase-review-status');
    badge.dataset.state = state;
    heads.append(node('h4', title), badge);
    item.append(heads, node('p', '原唱 ' + seconds(reference.start) + '–' + seconds(reference.end) + ' 秒', 'subtle'));
    const evidence = feedback?.evidence || feedback?.match;
    if (state === 'improve') {
      const direction = feedback.higher ? '偏高' : '偏低';
      const adjustment = feedback.higher ? '稍微降低' : '稍微抬高';
      item.append(node('p', '约 ' + seconds(evidence.take) + ' 秒的稳定音比原唱' + direction + '约 ' + feedback.difference + ' 个半音。'));
      item.append(node('p', '先听原唱这一处，再轻唱一次，把这个音' + adjustment + '，唱完和原唱对听。'));
    } else if (state === 'match') {
      item.append(node('p', '约 ' + seconds(evidence.take) + ' 秒有一小段稳定音贴近原唱。下一遍可以保留这处唱法。'));
    } else {
      item.append(node('p', feedback?.reason || '这句还没有可靠的对应片段。可在两轨选中同一句再比较。'));
    }
    const actions = node('div', '', 'button-row');
    if (evidence) {
      actions.append(button('听原唱对照点', () => evidencePlayback('reference', evidence.reference, reference)),
        button('听自己对照点', () => evidencePlayback('take', evidence.take, take)));
    } else {
      actions.append(button('听这句原唱', () => void play('reference', reference, 1)));
      if (take) actions.append(button('听个人候选片段', () => void play('take', take, 1)));
    }
    item.append(actions);
    const commentary = line && explainLine(line, $('genre').value);
    if (commentary) {
      const details = node('details', '', 'personal-disclosure');
      const link = node('a', commentary.source.name);
      link.href = commentary.source.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      details.append(node('summary', '展开这句的文学讲解'), node('p', commentary.explanation), link);
      item.append(details);
    }
    panel.append(item);
  });
  panel.hidden = false;
}

function drawSegments() {
  const panel = tracks.reference.parts.segments;
  panel.replaceChildren();
  panel.hidden = !loaded.reference;
  if (panel.hidden) return;
  const actions = node('div', '', 'button-row');
  actions.append(worker?.type === 'segments'
    ? button('取消分段', () => { cancelAnalysis(); notice(tracks.reference.parts.status, '已取消分段。'); }, 'button quiet')
    : button('找可练片段', () => startAnalysis('segments'), 'button quiet'));
  if (loaded.reference.source === 'example') {
    exampleRanges.forEach(([start,end], index) => actions.append(button('示例 ' + (index + 1), () => selectRange('reference', start, end), 'button quiet')));
  }
  if (segments.length) {
    panel.append(node('p', '检测到的音量区间，可点选后自行微调。', 'subtle'));
    for (const {start,end} of segments) {
      actions.append(button(seconds(start) + '–' + seconds(end) + ' 秒', () => selectRange('reference', start, end), 'button quiet'));
    }
  }
  panel.append(actions);
}

function startAnalysis(type) {
  const isReport = type !== 'segments';
  if (capture || !loaded.reference || (isReport && (!loaded.take || viewingPrevious))) return;
  const limit = type === 'single' ? 12000 : REPORT_LIMIT_MS;
  if (isReport && [loaded.reference, loaded.take].some(track => track.end - track.start > limit)) {
    showReport({rows:[], reason:'请在两条音轨分别选中同一内容，且每段不超过 ' + (limit / 1000) + ' 秒，再生成点评。'});
    return;
  }
  cancelAnalysis();
  if (isReport) { $('result').replaceChildren(); $('result').hidden = true; }
  let instance;
  try { instance = new Worker(new URL('./analysis-worker.js', import.meta.url), {type:'module'}); }
  catch {
    notice($('analysis-status'), '当前浏览器无法运行分析，仍可手工选段和对听。', true);
    return;
  }
  const task = {instance,type};
  worker = task;
  const reference = analysisClip(type === 'segments'
    ? {...loaded.reference, start:0, end:loaded.reference.duration} : loaded.reference);
  const take = isReport ? analysisClip(loaded.take) : null;
  const finish = result => {
    if (worker !== task) return;
    cancelAnalysis();
    if (isReport) {
      showReport(result);
      notice($('analysis-status'), result.rows.length ? '点评已生成，共 ' + result.rows.length + ' 个句候选。' : '暂未生成逐句点评。');
    }
    else {
      segments = Array.isArray(result) ? result : [];
      drawSegments();
      notice(tracks.reference.parts.status, segments.length
        ? '已找出 ' + segments.length + ' 个活动区间。请选择后试听确认。'
        : '没有找到可用的活动区间，可以手工选段。');
    }
  };
  instance.onmessage = event => finish(event.data);
  instance.onerror = () => finish({rows:[], reason:'本机分析暂不可用，可以继续对听。'});
  task.timeout = setTimeout(() => finish({rows:[], reason:'本机分析超时，可以缩短选段再试。'}), 20000);
  if (isReport) notice($('analysis-status'), '正在本机分句并比较演唱…');
  else notice(tracks.reference.parts.status, '正在检测音频区间…');
  drawSegments();
  controls();
  instance.postMessage({type,reference,take}, take ? [reference.samples.buffer,take.samples.buffer] : [reference.samples.buffer]);
}

async function refreshProjects() {
  let items;
  try { items = await getProjects(); }
  catch { notice($('storage-status'), '当前浏览器无法使用本机存储，仍可临时练习。', true); return; }
  const list = $('project-list');
  list.replaceChildren();
  for (const item of items.sort((a,b) => b.updated - a.updated)) {
    const entry = node('li');
    const label = node('strong', item.name);
    const savedMedia = ['reference','take','previousTake'].filter(role => item[role]?.blob).length;
    const state = node('p', savedMedia ? '已保存 ' + savedMedia + ' 条音轨' : '已记住位置 · 打开后可重选文件', 'subtle');
    const actions = node('div', '', 'button-row');
    actions.append(button('继续练', () => restore(item.id)), button('删除', () => deleteSaved(item.id), 'button quiet danger'));
    entry.append(label,state,actions);
    list.append(entry);
  }
  controls();
}

async function lockStorage(work) {
  if (storageBusy) return;
  storageBusy = true;
  for (const role of Object.keys(tracks)) renderTrack(role);
  controls();
  try { await work(); }
  catch (error) {
    notice($('storage-status'), error.name === 'QuotaExceededError'
      ? '本机空间不足，之前的项目已保留。可以仅保存位置。'
      : error.message || '本机存储操作失败。', true);
  } finally {
    storageBusy = false;
    for (const role of Object.keys(tracks)) renderTrack(role);
    controls();
  }
}

async function remember() {
  return lockStorage(async () => {
    const includeMedia = $('save-mode').value === 'media';
    const snapshot = {id:project.id, version:project.version,
      name:$('project-name').value.trim() || loaded.reference?.name || '个人练唱',
      transcript:$('transcript').value.trim(), genre:$('genre').value.trim(),
      rate:Number($('rate').value), repeats:Number($('repeats').value),
      reference:trackRecord(loaded.reference,includeMedia) || trackRecord(project.reference,includeMedia),
      take:trackRecord(loaded.take,includeMedia) || trackRecord(project.take,includeMedia),
      previousTake:includeMedia && $('keep-previous').checked ? trackRecord(previousTake,true) : null,
      updated:Date.now()};
    project.version = await saveProject(snapshot);
    await refreshProjects();
    notice($('storage-status'), includeMedia ? '已保存本机项目及可用音频。' : '已保存本机位置和唱词。');
  });
}

function clearSession() {
  epoch++;
  stopPlayback();
  cancelCapture('');
  invalidateAnalysis();
  loaded = {};
  previousTake = null;
  viewingPrevious = false;
  segments = [];
  project = newProject();
  for (const role of Object.keys(tracks)) {
    const slot = tracks[role];
    slot.sequence++;
    slot.busy = false;
    slot.drag = null;
    notice(slot.parts.status, '');
    renderTrack(role);
  }
  $('project-name').value = $('transcript').value = $('genre').value = '';
  $('save-mode').value = 'position';
  $('keep-previous').checked = false;
  $('rate').value = '1';
  $('repeats').value = '1';
  updateRateNote();
  syncProjectLabel();
  drawSegments();
  controls();
}

async function restore(id) {
  if (capture || Object.values(tracks).some(track => track.busy)) return;
  if ((loaded.reference || loaded.take) && !confirm('打开保存的练习会替换当前页面的音频，继续吗？')) return;
  await lockStorage(async () => {
    const original = await getProject(id);
    if (!original) { await refreshProjects(); throw new Error('这个项目已被删除。'); }
    const attempt = epoch;
    notice($('storage-status'), '正在打开本机练习…');
    const decoded = {};
    for (const role of Object.keys(roles)) {
      const saved = original[role];
      if (saved?.blob) {
        decoded[role] = await decodeTrack(saved.blob, saved.name, saved.source);
      } else if (saved?.source === 'example') decoded[role] = await openExample();
    }
    const savedPrevious = original.previousTake;
    let decodedPrevious = null;
    if (savedPrevious?.blob) {
      try {
        const track = await decodeTrack(savedPrevious.blob, savedPrevious.name, savedPrevious.source);
        if (track.fingerprint === savedPrevious.fingerprint && savedPrevious.start >= 0
            && savedPrevious.end <= track.duration && savedPrevious.end - savedPrevious.start >= 100) {
          decodedPrevious = {...track, id:savedPrevious.id, start:savedPrevious.start, end:savedPrevious.end,
            phrase:savedPrevious.phrase ?? null};
        }
      } catch { /* The current tracks remain usable even if the older recording cannot be decoded. */ }
    }
    if (attempt !== epoch) return;
    clearSession();
    project = {...original};
    for (const role of Object.keys(roles)) {
      if (decoded[role]) setTrack(role, decoded[role], original[role]);
    }
    previousTake = decodedPrevious;
    $('project-name').value = original.name;
    $('transcript').value = original.transcript || '';
    $('genre').value = original.genre || '';
    $('rate').value = String(original.rate || 1);
    $('repeats').value = String(original.repeats || 1);
    updateRateNote();
    $('save-mode').value = [original.reference,original.take,original.previousTake].some(item => item?.blob) ? 'media' : 'position';
    $('keep-previous').checked = !!decodedPrevious;
    syncProjectLabel();
    notice($('storage-status'), savedPrevious?.blob && !decodedPrevious
      ? '项目已打开，上一遍录音暂时无法播放。'
      : '本机项目已打开。只记了位置的轨道可重新选择文件。');
    $('practice-title').focus();
  });
}

async function deleteSaved(id) {
  if (!confirm('删除所选本机项目、录音和练习记录？')) return;
  await lockStorage(async () => {
    await eraseProject(id);
    if (id === project.id) clearSession();
    await refreshProjects();
    notice($('storage-status'), '项目已删除。');
  });
}

async function removeTrack(role) {
  if (capture || !confirm(role === 'previousTake'
    ? '删除上一遍录音及本机项目中保存的副本？'
    : '移除这条音轨，并清除当前项目中已保存的该轨？')) return;
  await lockStorage(async () => {
    if (project.version !== null) {
      const current = await getProject(project.id);
      if (current?.[role]) {
        current[role] = null;
        project.version = await saveProject({...current, version:project.version, updated:Date.now()});
      } else if (!current) project = newProject();
    }
    stopPlayback();
    invalidateAnalysis();
    if (role === 'previousTake') {
      previousTake = null;
      $('keep-previous').checked = false;
    } else delete loaded[role];
    viewingPrevious = false;
    project[role] = null;
    if (role === 'reference') { segments = []; drawSegments(); }
    const trackRole = role === 'previousTake' ? 'take' : role;
    renderTrack(trackRole);
    if (role === 'reference') renderTrack('take');
    syncProjectLabel();
    notice(tracks[trackRole].parts.status, role === 'previousTake' ? '上一遍录音已删除。' : '音轨已移除。');
    await refreshProjects();
  });
}

for (const [role, {parts}] of Object.entries(tracks)) {
  parts.import.addEventListener('click', () => parts.file.click());
  parts.file.addEventListener('change', () => {
    const file = parts.file.files[0];
    parts.file.value = '';
    void importTrack(role,file);
  });
  parts.play.addEventListener('click', () => void play(role));
  parts.remove.addEventListener('click', () => void removeTrack(role));
  parts.apply.addEventListener('click', () => {
    if (!parts.start.value || !parts.end.value) {
      notice(parts.status, '请填写起点和终点。', true);
      return;
    }
    selectRange(role, Math.round(Number(parts.start.value) * 1000), Math.round(Number(parts.end.value) * 1000));
  });
  parts.reset.addEventListener('click', () => selectRange(role,0,loaded[role].duration));
  parts.wave.addEventListener('pointerdown', event => {
    if (!loaded[role] || !event.isPrimary || event.button !== 0 || storageBusy || capture || (role === 'take' && viewingPrevious)) return;
    tracks[role].drag = locateInWave(role, parts.wave, event);
    parts.wave.setPointerCapture(event.pointerId);
  });
  parts.wave.addEventListener('pointerup', event => {
    const first = tracks[role].drag;
    tracks[role].drag = null;
    if (first === null || !loaded[role]) return;
    const last = locateInWave(role, parts.wave, event);
    if (Math.abs(first - last) >= 100) selectRange(role,Math.min(first,last),Math.max(first,last));
    else drawTrack(role);
  });
  parts.wave.addEventListener('pointermove', event => {
    const first = tracks[role].drag;
    if (first === null || !loaded[role] || !parts.wave.hasPointerCapture(event.pointerId)) return;
    const last = locateInWave(role, parts.wave, event);
    drawTrack(role,null,[Math.min(first,last),Math.max(first,last)]);
  });
  parts.wave.addEventListener('lostpointercapture', () => {
    if (tracks[role].drag !== null) { tracks[role].drag = null; drawTrack(role); }
  });
  renderTrack(role);
}

tracks.take.parts.record.addEventListener('click', requestCapture);
tracks.take.parts.advice.addEventListener('click', () => $('analysis-title').focus());
tracks.take.parts.capture.addEventListener('click', () => void finishCapture());
tracks.take.parts.cancel.addEventListener('click', () => cancelCapture('本次录音已取消。'));
for (const [part, previous] of [['current',false],['previous',true]]) {
  tracks.take.parts[part].addEventListener('click', () => {
    stopPlayback();
    viewingPrevious = previous;
    renderTrack('take');
    controls();
  });
}
tracks.take.parts['remove-previous'].addEventListener('click', () => void removeTrack('previousTake'));
$('record-consent').addEventListener('close', event => {
  if (event.currentTarget.returnValue === 'record') {
    explainedMic = true;
    void beginCapture();
  }
});
$('practice-title').tabIndex = -1;
$('hero-import').addEventListener('click', () => tracks.reference.parts.file.click());
$('open-example').addEventListener('click', () => void loadExample());
$('stop-play').addEventListener('click', stopPlayback);
$('play-lead').addEventListener('click', () => {
  const {start, end} = loaded.reference;
  void play('reference', {start:Math.max(0, start - 1500), end:Math.min(end, start + 400)}, 1, '听前导');
});
$('rate').addEventListener('change', () => {
  stopPlayback();
  updateRateNote();
});
$('new-project').addEventListener('click', () => {
  if (confirm('开始新的练习？当前页面未保存的修改将被清除。')) {
    clearSession();
    $('hero-import').focus();
  }
});
$('save-mode').addEventListener('change', controls);
$('analyze').addEventListener('click', () => startAnalysis('report'));
$('analyze-single').addEventListener('click', () => {
  if (loaded.take && loaded.reference) loaded.take.phrase = phraseKey();
  renderTrack('take');
  startAnalysis('single');
});
$('cancel-analysis').addEventListener('click', () => cancelAnalysis('已取消，可以继续对听。'));
$('save').addEventListener('click', () => void remember());
$('project-name').addEventListener('input', syncProjectLabel);
for (const id of ['genre','transcript']) $(id).addEventListener('input', invalidateAnalysis);
document.addEventListener('visibilitychange', () => {
  if (document.hidden) {
    if (capture) cancelCapture('离开页面后录音已结束，可以重录。');
    stopPlayback();
  }
});
window.addEventListener('pagehide', () => { clearSession(); void player.close(); });
window.addEventListener('pageshow', event => { if (event.persisted) void refreshProjects(); });

controls();
void refreshProjects();
if ('serviceWorker' in navigator) {
  const serviceWorker = location.pathname === '/' ? '/personal-sw.js?personal-root=1' : '/personal-sw.js';
  navigator.serviceWorker.register(serviceWorker).then(() => navigator.serviceWorker.ready)
    .then(() => { $('offline-status').textContent = '离线资源已就绪'; })
    .catch(() => { $('offline-status').textContent = '离线资源暂未就绪'; });
}
