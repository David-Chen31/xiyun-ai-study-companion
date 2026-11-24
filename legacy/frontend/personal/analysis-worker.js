import { findActivity, phraseReport } from './analysis.js';

self.onmessage = event => {
  const {reference, take, type} = event.data;
  try {
    self.postMessage(type === 'segments' ? findActivity(reference) : phraseReport(reference, take, type === 'single'));
  } catch {
    self.postMessage({rows:[], reason:'本机分析未完成。你仍可选择片段对听。'});
  }
};
