import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { audioContour, audioEnvelope, CONTOUR_STEP_MS } from '../frontend/audio/features.js';

const [, , songId, audioFile, cuesFile, output, analysisFile = audioFile, wordsFile] = process.argv;
const sampleRate = 24000;
if (!songId || !audioFile || !cuesFile || !output) {
  throw new Error('用法：node tools/prepare_track.mjs 曲目ID 原唱.mp3 唱词.srt 输出.json [参考人声音频] [审核过的逐字时间 JSON]');
}

const run = (program, args) => {
  const result = spawnSync(program, args, {maxBuffer:32 * 1024 * 1024, windowsHide:true});
  if (result.error) throw result.error;
  if (result.status !== 0) throw new Error(result.stderr.toString('utf8'));
  return result.stdout;
};
const stamp = value => {
  const [hours, minutes, seconds] = value.replace(',', '.').split(':').map(Number);
  return Math.round((hours * 3600 + minutes * 60 + seconds) * 1000);
};
const duration = Math.round(Number(run('ffprobe', ['-v','error','-show_entries','format=duration',
  '-of','default=noprint_wrappers=1:nokey=1',audioFile]).toString()) * 1000);
if (!Number.isFinite(duration) || duration <= 0 || duration > 600000) throw new Error('音频须在十分钟以内。');
const cues = readFileSync(cuesFile, 'utf8').replace(/^\uFEFF/, '').trim().split(/\r?\n\s*\r?\n/);
const lines = [];
for (const cue of cues) {
  const [, times, ...body] = cue.trim().split(/\r?\n/);
  const [start, end] = (times || '').split(' --> ').map(stamp);
  const text = body.join(' ').trim();
  if (!text || !Number.isFinite(start) || !Number.isFinite(end)) continue;
  const start_ms = Math.max(lines.at(-1)?.end_ms || 0, start);
  const end_ms = Math.min(duration, end);
  if (end_ms - start_ms < 180) continue;
  lines.push({id:'line-' + (lines.length + 1), start_ms, end_ms, text});
}
if (!lines.length) throw new Error('没有可用的 SRT 唱词时间点。');
if (wordsFile) {
  const words = JSON.parse(readFileSync(wordsFile, 'utf8'));
  for (const {line_id, text, start_ms, end_ms, pinyin} of words) {
    const line = lines.find(item => item.id === line_id);
    if (!line || typeof text !== 'string' || !text || !line.text.includes(text)
      || !Number.isInteger(start_ms) || !Number.isInteger(end_ms)
      || start_ms < (line.words?.at(-1)?.end_ms ?? line.start_ms) || end_ms <= start_ms || end_ms > line.end_ms
      || (pinyin !== undefined && (typeof pinyin !== 'string' || pinyin.length > 32))) {
      throw new Error('逐字时间与唱词不匹配：' + line_id);
    }
    (line.words ??= []).push({text, start_ms, end_ms, pinyin});
  }
}
function decode(path) {
  const pcm = run('ffmpeg', ['-v','error','-i',path,'-vn','-ac','1','-ar',String(sampleRate),'-f','s16le','pipe:1']);
  const samples = new Float32Array(Math.floor(pcm.length / 2));
  for (let i = 0; i < samples.length; i++) samples[i] = pcm.readInt16LE(i * 2) / 32768;
  return samples;
}
const samples = decode(analysisFile);
if (Math.abs(samples.length * 1000 / sampleRate - duration) > 750) throw new Error('参考人声音频需与原唱保持相同时间轴。');
const mixed = analysisFile === audioFile ? samples : decode(audioFile);
const contour = audioContour(samples, sampleRate);
const mixedContour = mixed === samples ? null : audioContour(mixed, sampleRate);
const waveform = audioEnvelope(mixed, sampleRate);
for (const line of lines) {
  line.reference = contour.filter(([ms]) => ms >= line.start_ms && ms < line.end_ms);
  line.waveform = waveform.filter(([ms]) => ms >= line.start_ms - CONTOUR_STEP_MS && ms < line.end_ms);
  if (mixedContour) {
    line.mix_reference = mixedContour.filter(([ms]) => ms >= line.start_ms && ms < line.end_ms);
  }
}
const profile = {
  id:songId,
  duration_ms:duration,
  profile_version:2,
  contour_step_ms:CONTOUR_STEP_MS,
  lyrics_source:'预制本地时间轴',
  reference_source:analysisFile === audioFile ? 'mix' : 'vocal',
  source_sha256:createHash('sha256').update(readFileSync(audioFile)).digest('hex'),
  lines
};
mkdirSync(dirname(output), {recursive:true});
writeFileSync(output + '.tmp', JSON.stringify(profile));
renameSync(output + '.tmp', output);
console.log(JSON.stringify({lines:lines.length, duration_ms:duration, frames:contour.length,
  voiced_frames:contour.filter(point => point[1]).length}));
