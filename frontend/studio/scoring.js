import { CONTOUR_STEP_MS } from '../audio/features.js';

const semitones = (a, b) => 12 * Math.log2(a / b);
const median = numbers => [...numbers].sort((a, b) => a - b)[Math.floor(numbers.length / 2)];
const noteName = hz => {
  const pitch = Math.round(69 + semitones(hz, 440));
  return ['C','C♯','D','D♯','E','F','F♯','G','G♯','A','A♯','B'][pitch % 12] + (Math.floor(pitch / 12) - 1);
};

function isStable(points, index, tolerance) {
  const frame = points[index];
  if (!frame?.[1] || frame[2] < -44) return false;
  return [points[index - 1], points[index + 1]].some(neighbor => neighbor?.[1]
    && Math.abs(neighbor[0] - frame[0]) <= CONTOUR_STEP_MS + 4
    && Math.abs(semitones(neighbor[1], frame[1])) <= tolerance);
}

function compareAt(line, points, contour, offset, duration, minimum, scale) {
  const raw = [];
  for (const [ms, hz] of points) {
    const takeMs = line.start_ms - offset + (ms - line.start_ms) * scale;
    if (takeMs < 0 || takeMs >= duration) continue;
    const index = Math.max(0, Math.round((takeMs - 28) / CONTOUR_STEP_MS));
    if (!isStable(contour, index, 1.1)) continue;
    const [time, frequency] = contour[index];
    const delta = semitones(frequency, hz);
    if (Math.abs(delta) <= 18) raw.push({delta, reference_ms:ms, take_ms:time, reference_hz:hz});
  }
  if (raw.length < minimum) return null;
  const register = [-12, 12].find(octave =>
    raw.filter(frame => Math.abs(frame.delta - octave) <= 1.5).length >= Math.max(4, Math.ceil(raw.length * .7))) ?? 0;
  const comparisons = raw.map(frame => ({...frame, delta:frame.delta - register}))
    .filter(frame => Math.abs(frame.delta) <= 7);
  if (comparisons.length < minimum ||
      comparisons.at(-1).reference_ms - comparisons[0].reference_ms < 2 * CONTOUR_STEP_MS) return null;
  const deviation = median(comparisons.map(frame => Math.abs(frame.delta)));
  const coverage = comparisons.length / points.length;
  return {comparisons, deviation, coverage, register, offset, scale,
    cost:deviation + (1 - coverage) * 1.8 + (register ? .18 : 0) + Math.abs(scale - 1) * 2.5};
}

function offPitchRun(comparisons) {
  let run = [], longest = [];
  for (const frame of comparisons) {
    const previous = run.at(-1);
    if (Math.abs(frame.delta) < 1.15) { run = []; continue; }
    if (previous && (Math.sign(frame.delta) !== Math.sign(previous.delta)
      || frame.reference_ms - previous.reference_ms > CONTOUR_STEP_MS * 1.6)) run = [];
    run.push(frame);
    if (run.length > longest.length) longest = [...run];
  }
  return longest.length >= 3 ? longest : [];
}

export function scoreLines(lines, contour, duration, {origin, guided}) {
  return lines.map(line => {
    if (line.end_ms <= origin || line.start_ms >= origin + duration) {
      return {id:line.id, status:'outside', description:'这次录音没有覆盖这一句。'};
    }
    const points = line.reference.filter((_, index, all) => isStable(all, index, .95));
    if (points.length < 4) {
      return {id:line.id, status:'unavailable', description:'这一句可用的原唱音高太少，可以先对照回听。'};
    }
    let aligned = null;
    const radius = guided ? 3 : 8;
    const minimum = Math.max(4, Math.ceil(points.length * .2));
    for (const scale of (guided ? [1] : [1, .93, 1.07])) {
      for (let shift = -radius; shift <= radius; shift++) {
        const candidate = compareAt(line, points, contour, origin + shift * CONTOUR_STEP_MS, duration, minimum, scale);
        if (candidate) {
          candidate.cost += Math.abs(shift) * .055;
          if (!aligned || candidate.cost < aligned.cost) aligned = candidate;
        }
      }
    }
    if (!aligned) return {id:line.id, status:'unavailable',
      description:'两段能对上的稳定人声太少。可以检查录音起点，或戴耳机重录。'};
    const {comparisons, deviation, coverage, register, offset, scale} = aligned;
    const close = comparisons.filter(frame => Math.abs(frame.delta) <= .75);
    const off = offPitchRun(comparisons);
    const evidence = off.length ? off : close.length ? close : comparisons;
    const point = evidence[Math.floor(evidence.length / 2)];
    const near = Math.round(close.length * 100 / comparisons.length);
    const score = Math.max(0, Math.min(100, Math.round(100 - deviation * 11 - (100 - near) * .23)));
    const fraction = (point.reference_ms - line.start_ms) / (line.end_ms - line.start_ms);
    const position = fraction > .7 ? '句尾' : fraction < .3 ? '句首' : '句中';
    const sustained = off.length ? median(off.map(frame => frame.delta)) : null;
    const facts = {pitch_score:score, coverage:Math.round(coverage * 100), matched_percent:near, position};
    if (register) facts.register = register;
    if (!register && off.length) {
      facts.target_note = noteName(point.reference_hz);
      facts.delta_cents = Math.round(sustained * 100);
      facts.duration_ms = off.at(-1).reference_ms - off[0].reference_ms + CONTOUR_STEP_MS;
      facts.spread_cents = Math.round((Math.max(...off.map(frame => frame.delta))
        - Math.min(...off.map(frame => frame.delta))) * 100);
      const word = line.words?.find(token => token.start_ms <= point.reference_ms && point.reference_ms < token.end_ms
        && off.filter(frame => token.start_ms <= frame.reference_ms && frame.reference_ms < token.end_ms).length >= 3);
      if (word) {
        facts.aligned_word = word.text;
        facts.reference_ms = point.reference_ms;
      }
    }
    let description = near >= 72 ? '可比较的音高大多贴近原唱。' : '这一句的音高还有些浮动。';
    let advice = near >= 72 ? '' : '交替听两段，选出最难的一个音。先轻哼对准，再带词唱。';
    if (register) {
      description = '两段旋律大致在不同八度。分数按相同音区换算。';
      advice = '用你舒服的音区练，留意旋律的高低走向。';
    } else if (off.length) {
      const higher = point.delta > 0;
      const target = facts.aligned_word ? '「' + facts.aligned_word + '」字' : position + '的一小段';
      description = target + '持续' + (higher ? '偏高' : '偏低') + '，约 ' + Math.abs(facts.delta_cents) + ' 音分。';
      advice = '听两条音浪上的标记位置。先轻哼这一处，' + (higher ? '稍收低' : '稍抬高') + '，再把词带回去。';
    }
    return {id:line.id, status:'scored', score, coverage:Math.round(coverage * 100), description, advice,
      reference_ms:point.reference_ms, take_ms:point.take_ms, offset, scale, facts};
  });
}
