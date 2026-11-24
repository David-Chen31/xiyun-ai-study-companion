function rms(samples, start, length) {
  let sum = 0;
  for (let i = start; i < start + length; i++) sum += samples[i] ** 2;
  return Math.sqrt(sum / length);
}

function downsample(clip) {
  const stride = Math.max(1, Math.round(clip.sampleRate / 8000));
  const samples = new Float32Array(Math.floor(clip.samples.length / stride));
  for (let i = 0; i < samples.length; i++) {
    let sum = 0;
    for (let j = 0; j < stride; j++) sum += clip.samples[i * stride + j];
    samples[i] = sum / stride;
  }
  return {samples, rate:clip.sampleRate / stride};
}

function estimatePitch(frame, rate, minimumCorrelation = .9) {
  if (rms(frame, 0, frame.length) < .009) return null;
  const centered = Float32Array.from(frame);
  const average = centered.reduce((sum, value) => sum + value, 0) / centered.length;
  for (let i = 0; i < centered.length; i++) centered[i] -= average;
  const minimum = Math.max(2, Math.floor(rate / 900));
  const maximum = Math.ceil(rate / 80);
  const correlations = [];
  for (let lag = minimum - 1; lag <= maximum + 1; lag++) {
    let cross = 0, a = 0, b = 0;
    for (let i = 0; i < centered.length - lag; i++) {
      const left = centered[i], right = centered[i + lag];
      cross += left * right;
      a += left * left;
      b += right * right;
    }
    correlations[lag] = a && b ? cross / Math.sqrt(a * b) : 0;
  }
  const highest = Math.max(...correlations.slice(minimum, maximum + 1));
  if (highest < minimumCorrelation) return null;
  for (let lag = minimum; lag <= maximum; lag++) {
    const middle = correlations[lag], left = correlations[lag - 1], right = correlations[lag + 1];
    if (middle < Math.max(minimumCorrelation, highest * .96) || middle < left || middle <= right) continue;
    const curve = left - 2 * middle + right;
    const delta = curve ? .5 * (left - right) / curve : 0;
    const hz = rate / (lag + delta);
    if (hz >= 80 && hz <= 900) return hz;
  }
  return null;
}

function pitchTrack(clip) {
  const {samples, rate} = downsample(clip);
  const frame = Math.round(rate * .048), step = Math.round(rate * .045);
  const points = [];
  for (let i = 0; i + frame <= samples.length; i += step) {
    points.push({ms:Math.round((i + frame / 2) * 1000 / rate),
      hz:estimatePitch(samples.subarray(i, i + frame), rate)});
  }
  return points;
}

export const CONTOUR_STEP_MS = 90;

export function audioEnvelope(samples, sampleRate) {
  const size = Math.max(1, Math.round(sampleRate * CONTOUR_STEP_MS / 1000));
  const points = [];
  for (let start = 0; start < samples.length; start += size) {
    const amplitude = rms(samples, start, Math.min(size, samples.length - start));
    points.push([Math.round(start * 1000 / sampleRate), Math.round(amplitude * 10000) / 10000]);
  }
  return points;
}

export function audioContour(samples, sampleRate) {
  const reduced = downsample({samples, sampleRate});
  const {rate} = reduced;
  const width = Math.round(rate * .055), step = Math.round(rate * CONTOUR_STEP_MS / 1000);
  const contour = [];
  for (let start = 0; start + width <= reduced.samples.length; start += step) {
    const frame = reduced.samples.subarray(start, start + width);
    const hz = estimatePitch(frame, rate, .72);
    const level = Math.max(-90, Math.round(20 * Math.log10(rms(frame, 0, width))));
    contour.push([Math.round((start + width / 2) * 1000 / rate), hz ? Math.round(hz * 10) / 10 : null, level]);
  }
  return contour;
}

const semitones = (a, b) => 12 * Math.log2(a / b);
const stable = points => points.every(point => point.hz && Math.abs(semitones(point.hz, points[0].hz)) < .75);

export function compare(reference, take) {
  const duration = clip => clip.samples.length * 1000 / clip.sampleRate;
  const leftDuration = duration(reference), rightDuration = duration(take);
  if (Math.min(leftDuration, rightDuration) < 650 || Math.max(leftDuration, rightDuration) > 12000) {
    return {status:'unavailable', reason:'请在两条音轨各选 0.65–12 秒的同一句。'};
  }
  const ratio = rightDuration / leftDuration;
  if (ratio < .8 || ratio > 1.25) {
    return {status:'unavailable', reason:'两段时长相差较大。请先调整两边的起止位置，再进行对比。'};
  }
  const left = pitchTrack(reference), right = pitchTrack(take);
  const coverage = track => track.filter(point => point.hz).length / track.length;
  if (coverage(left) < .5 || coverage(right) < .5) {
    return {status:'unavailable', reason:'可连续跟踪的音高较少。请试试人声清楚、伴奏较轻的短句。'};
  }

  let difference = null;
  let match = null;
  let matchedSince = null;
  let incompatible = false;
  for (let index = 0; index + 4 < left.length; index++) {
    const start = left[index].ms / leftDuration;
    const end = left[index + 4].ms / leftDuration;
    const a = left.slice(index, index + 5);
    const b = right.filter(point => point.ms / rightDuration >= start && point.ms / rightDuration <= end);
    if (b.length < 3 || !stable(a) || !stable(b)) { matchedSince = null; continue; }
    const middle = b[Math.floor(b.length / 2)];
    const delta = semitones(middle.hz, a[2].hz);
    const magnitude = Math.abs(delta);
    const evidence = {reference:reference.start + a[2].ms, take:take.start + middle.ms};
    if (magnitude >= 1.1 && magnitude <= 7 && (!difference || magnitude > difference.magnitude)) {
      difference = {evidence, magnitude, higher:delta > 0};
    }
    if (magnitude > 7) incompatible = true;
    if (magnitude <= .5) {
      matchedSince ??= a[0].ms;
      if (a.at(-1).ms - matchedSince >= 360) match = evidence;
    } else {
      matchedSince = null;
    }
  }
  if (incompatible) return {status:'unavailable', reason:'部分稳定音相差较大。请检查选段、调高和对应关系。'};
  if (difference) {
    return {status:'comparison', left, right, evidence:difference.evidence,
      higher:difference.higher, difference:Math.round(difference.magnitude * 10) / 10, match};
  }
  return {status:'no-difference', left, right, match,
    reason:'这次没有找到满足条件的局部音高差异。可以回听确认，或另选一句。'};
}

export function findActivity(clip, {minGap = 10, limit = 12} = {}) {
  const {samples, rate} = downsample(clip);
  const width = Math.round(rate * .025), levels = [];
  for (let i = 0; i + width <= samples.length; i += width) levels.push(rms(samples, i, width));
  if (!levels.length) return [];
  const peak = levels.reduce((a, b) => Math.max(a, b), 0);
  if (peak < .008) return [];
  const ordered = [...levels].sort((a, b) => a - b);
  const threshold = Math.max(.006, peak * .1, Math.min(ordered[Math.floor(ordered.length * .2)] * 2.5, peak * .42));
  const active = [];
  levels.forEach((level, index) => { if (level >= threshold) active.push(index); });
  const ranges = [];
  let first = null, last = null;
  function finish() {
    if (first !== null && last - first >= 8) {
      ranges.push({start:clip.start + Math.max(0, first - 4) * 25,
        end:clip.start + Math.min(levels.length, last + 4) * 25});
    }
  }
  for (const i of active) {
    if (last === null || i - last > minGap) { finish(); first = i; }
    last = i;
  }
  finish();
  return ranges.slice(0, limit);
}

export const REPORT_LIMIT_MS = 90000;
const MAX_REPORT_PHRASES = 16;

function clipRange(clip) {
  return {start:clip.start, end:clip.start + Math.floor(clip.samples.length * 1000 / clip.sampleRate)};
}

function sliceClip(clip, range) {
  const index = ms => Math.round((ms - clip.start) * clip.sampleRate / 1000);
  return {...clip, start:range.start, samples:clip.samples.subarray(index(range.start), index(range.end))};
}

export function phraseReport(reference, take, single = false) {
  const referenceRange = clipRange(reference);
  const takeRange = clipRange(take);
  const duration = range => range.end - range.start;
  if (Math.max(duration(referenceRange), duration(takeRange)) > REPORT_LIMIT_MS) {
    return {reason:'请在两条音轨上选取同一段、各不超过 90 秒，再生成点评。', rows:[]};
  }
  if (single) return {rows:[{reference:referenceRange, take:takeRange, feedback:compare(reference, take)}]};
  const options = {minGap:19, limit:MAX_REPORT_PHRASES + 1};
  const left = findActivity(reference, options);
  const right = findActivity(take, options);
  if (!left.length || !right.length) {
    return {reason:'没有在两边都找到足够清楚的声音片段。可选中同一句再比较，或补录一次。', rows:[]};
  }
  if (Math.max(left.length, right.length) > MAX_REPORT_PHRASES) {
    return {reason:'这段包含较多停顿。请缩短选段，分几段查看点评。', rows:[]};
  }
  if (left.length !== right.length) return {
    reason:'两边按停顿分出的片段数不同，暂时无法逐句对应。可以先在两轨选中同一句，再点「只点评选中的一句」。',
    rows:left.map(reference => ({reference}))
  };
  const relativeEnd = (range, whole) => (range.end - whole.start) / duration(whole);
  return {rows:left.map((range, index) => {
    const candidate = right[index];
    if (Math.abs(relativeEnd(range, referenceRange) - relativeEnd(candidate, takeRange)) > .18) {
      return {reference:range, take:candidate};
    }
    const {left:referencePitch, right:takePitch, ...feedback} = compare(sliceClip(reference, range), sliceClip(take, candidate));
    return {reference:range, take:candidate, feedback};
  })};
}
