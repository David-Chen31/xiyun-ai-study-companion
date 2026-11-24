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
