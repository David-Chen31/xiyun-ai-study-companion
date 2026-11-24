import { audioContour } from '../audio/features.js';
import { scoreLines } from './scoring.js';

let acoustic = null;
let learned = null;
let duration = 0;

async function learnedContour(samples, sampleRate) {
  if (sampleRate !== 24000) return null;
  const pcm = new DataView(new ArrayBuffer(samples.length * 2));
  for (let i = 0; i < samples.length; i++) {
    pcm.setInt16(i * 2, Math.round(Math.max(-1, Math.min(1, samples[i])) * 32767), true);
  }
  try {
    const response = await fetch('/api/karaoke/pitch', {method:'POST', body:pcm.buffer,
      headers:{'Content-Type':'application/octet-stream'}, signal:AbortSignal.timeout(22000)});
    if (!response.ok) return null;
    const data = await response.json();
    if (data.engine === 'crepe-tiny' && Array.isArray(data.contour) && data.contour.length) return data.contour;
  } catch { /* The local acoustic method keeps practice working without the model. */ }
  return null;
}

self.onmessage = async ({data}) => {
  try {
    if (data.samples) {
      learned = data.modelReady ? await learnedContour(data.samples, data.sampleRate) : null;
      acoustic = audioContour(data.samples, data.sampleRate);
      duration = Math.floor(data.samples.length / data.sampleRate * 1000);
    }
    if (!acoustic) throw new Error('缺少录音数据。');
    const fallback = scoreLines(data.lines, acoustic, duration, data.timing);
    if (!learned) {
      self.postMessage({id:data.id, results:fallback});
      return;
    }
    const modelLines = data.lines.map(line => ({...line, reference:line.model_reference ?? []}));
    const primary = scoreLines(modelLines, learned, duration, data.timing);
    const results = primary.map((result, index) => {
      if (result.status === 'scored') return {...result, engine:'crepe-tiny'};
      return fallback[index].status === 'scored' ? fallback[index] : result;
    });
    self.postMessage({id:data.id, results});
  } catch (error) {
    self.postMessage({id:data.id, error:error.message});
  }
};
