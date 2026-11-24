import assert from 'node:assert/strict';
import test from 'node:test';
import { scoreLines } from '../frontend/studio/scoring.js';
import { audioContour } from '../frontend/audio/features.js';

function recording(hz) {
  const samples = Float32Array.from({length:16000}, (_, i) => .24 * Math.sin(2 * Math.PI * hz * i / 8000));
  return audioContour(samples, 8000);
}

const line = {id:'first', start_ms:0, end_ms:2000, reference:recording(262)};
const score = hz => scoreLines([line], recording(hz), 2000, {origin:0,guided:true})[0];

test('same stable pitch has an evidence-linked score', () => {
  const result = score(262);
  assert.equal(result.status, 'scored');
  assert.equal(result.score, 100);
  assert.ok(result.reference_ms > 0 && result.take_ms > 0);
});

test('off-pitch recording yields a directional practice action', () => {
  const result = score(294);
  assert.equal(result.status, 'scored');
  assert.ok(result.score < 90);
  assert.match(result.description, /偏高/);
  assert.match(result.advice, /收低/);
});

test('silence and non-overlapping intervals abstain', () => {
  assert.equal(score(0).status, 'unavailable');
  assert.equal(scoreLines([line], recording(262), 2000, {origin:4000,guided:false})[0].status, 'outside');
});

test('isolated octave jumps cannot turn into pitch advice', () => {
  const contour = recording(262);
  for (const point of contour.slice(9, 11)) point[1] /= 2;
  const result = scoreLines([line], contour, 2000, {origin:0,guided:true})[0];
  assert.equal(result.status, 'scored');
  assert.ok(result.score >= 95);
  assert.doesNotMatch(result.description, /偏低|偏高|半音/);
});

test('sustained octave differences are treated as register choices', () => {
  const result = score(131);
  assert.equal(result.status, 'scored');
  assert.ok(result.score >= 95);
  assert.match(result.description, /不同八度/);
  assert.doesNotMatch(result.advice, /抬高|压低/);
});

test('word-level advice requires verified timing spanning the measured error', () => {
  const contour = recording(294);
  assert.equal(scoreLines([line], contour, 2000, {origin:0,guided:true})[0].facts.aligned_word, undefined);
  const verified = {...line, words:[{text:'啊', start_ms:550, end_ms:1550}]};
  const result = scoreLines([verified], contour, 2000, {origin:0,guided:true})[0];
  assert.equal(result.facts.aligned_word, '啊');
  assert.equal(result.facts.target_note, 'C4');
  assert.ok(result.facts.delta_cents > 170);
});
