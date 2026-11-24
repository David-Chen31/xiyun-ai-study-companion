function formatSeconds(milliseconds) {
  return `${Number((milliseconds / 1000).toFixed(1))} 秒`;
}

export class PracticeView {
  #document;
  #window;
  #unbind = null;
  #noteId = null;
  #playback = {};
  #recording = {};
  #displacedAction = null;
  #waveformKey = null;
  #neighbors = [];

  constructor({ document, window }) {
    this.#document = document;
    this.#window = window;
  }
  #element(id) { return this.#document.getElementById(id); }
  #node(tag, className, ...children) {
    const node = this.#document.createElement(tag);
    if (className) node.className = className;
    node.append(...children);
    return node;
  }

  bind(actions) {
    this.unbind();
    const subscriptions = [];
    const listen = (target, event, handler) => {
      target.addEventListener(event, handler);
      subscriptions.push(() => target.removeEventListener(event, handler));
    };
    const picker = this.#element('phrase-picker');
    const wide = this.#window.matchMedia('(min-width: 900px)');
    const disclosures = [...this.#document.querySelectorAll('[data-disclosure]')];
    const showIndex = () => { picker.open = wide.matches; };
    showIndex();
    listen(wide, 'change', showIndex);
    for (const disclosure of disclosures) {
      listen(disclosure.querySelector('summary'), 'click', () => {
        if (disclosure.open) return;
        for (const other of disclosures) {
          if (other !== disclosure && (other !== picker || !wide.matches)) other.open = false;
        }
      });
      listen(disclosure, 'keydown', event => {
        if (event.key !== 'Escape' || !disclosure.open || (disclosure === picker && wide.matches)) return;
        disclosure.open = false;
        disclosure.querySelector('summary').focus();
      });
    }
    listen(this.#element('phrase-list'), 'click', event => {
      const button = event.target.closest('button[data-phrase-id]');
      if (!button) return;
      actions.select(button.dataset.phraseId);
      if (!wide.matches) picker.open = false;
      this.#element('current-text').focus();
    });
    ['previous-phrase', 'next-phrase'].forEach((id, index) => {
      listen(this.#element(id), 'click', () => {
        const phrase = this.#neighbors[index];
        if (phrase) {
          actions.select(phrase.id);
          this.#element('current-text').focus({preventScroll:true});
        }
      });
    });
    for (const [id, handler] of Object.entries({
      play: actions.play, stop: actions.stop, retry: actions.retry,
      'stop-recording': actions.stopRecording,
      'play-recording': actions.playRecording, 'delete-recording': actions.deleteRecording,
      'mark-bookmark': actions.mark,
      'clear-bookmark': () => {
        if (this.#window.confirm('清除此作业的待练位置？')) actions.clearBookmark();
      }
    })) listen(this.#element(id), 'click', handler);
    const consent = this.#element('recording-consent');
    const record = this.#element('record');
    const confirmation = this.#element('confirm-recording');
    let explained = false;
    listen(record, 'click', () => {
      if (explained) { actions.record(); return; }
      consent.hidden = false;
      this.#syncControls();
      confirmation.focus();
    });
    listen(confirmation, 'click', () => {
      explained = true;
      consent.hidden = true;
      this.#syncControls();
      record.focus();
      actions.record();
    });
    const cancelConsent = () => {
      consent.hidden = true;
      this.#syncControls();
      record.focus();
    };
    listen(this.#element('cancel-recording'), 'click', cancelConsent);
    listen(consent, 'keydown', event => { if (event.key === 'Escape') cancelConsent(); });
    const alternate = this.#element('play-alternative');
    if (alternate && actions.alternative) listen(alternate, 'click', actions.alternative);
    const questions = this.#element('ask-course');
    if (questions && actions.question) {
      listen(questions, 'submit', async event => {
        event.preventDefault();
        const button = questions.querySelector('button');
        const result = this.#element('question-result');
        button.disabled = true;
        result.textContent = '正在查询…';
        try {
          const response = await actions.question(this.#element('question-text').value);
          if (response.citations.length) {
            const entries = response.citations.map(item => {
              const details = this.#node('div', 'citation', this.#node('p', null, `${item.term} · ${item.definition}`),
                this.#node('p', 'subtle', `出处 · ${item.source}`));
              if (item.phrase_id) {
                const locate = this.#node('button', 'button quiet', '定位出处所在句');
                locate.type = 'button';
                locate.addEventListener('click', () => {
                  actions.select(item.phrase_id);
                  this.#element('practice')?.focus();
                });
                details.append(locate);
              }
              return details;
            });
            result.replaceChildren(...entries);
          } else result.textContent = response.reason;
        } catch (error) {
          result.textContent = error.message;
        } finally {
          button.disabled = false;
        }
      });
    }
    listen(this.#element('speed'), 'change', event => actions.rate(Number(event.target.value)));
    listen(this.#element('repeat'), 'change', event => actions.repeats(Number(event.target.value)));
    listen(this.#document, 'visibilitychange', () => this.#document.hidden ? actions.hide() : actions.show());
    listen(this.#window, 'pagehide', actions.leave);
    listen(this.#window, 'pageshow', event => { if (event.persisted) actions.show(); });
    if (actions.online) listen(this.#window, 'online', actions.online);
    this.#unbind = () => subscriptions.forEach(unsubscribe => unsubscribe());
  }
  unbind() { this.#unbind?.(); this.#unbind = null; }

  showNotice(title, description, retry = false, login = false, blocking = false) {
    this.#element('notice-title').textContent = title;
    this.#element('notice-description').textContent = description;
    this.#element('notice').hidden = false;
    this.#element('retry').hidden = !retry;
    const entry = this.#element('workspace-login');
    entry.hidden = !login;
    if (login) {
      const course = new URLSearchParams(this.#window.location.search).get('course');
      entry.href = course ? `/workspace/?return_course=${encodeURIComponent(course)}` : '/workspace/';
    }
    if (blocking) {
      this.#waveformKey = null;
      this.#element('score-waveform').hidden = true;
      this.#element('workspace').hidden = true;
      this.#element('recording-consent').hidden = true;
      this.#element('question-result').replaceChildren();
    }
  }
  clearNotice() { this.#element('notice').hidden = true; }
  setRetryBusy(busy) { this.#element('retry').disabled = busy; }

  #feedback(id, text) {
    const node = this.#element(id);
    if (node.textContent !== text) node.textContent = text;
    node.hidden = !text;
  }

  #syncControls(focused = this.#document.activeElement) {
    const {phase, canPlay, canStop} = this.#playback;
    const {phase: capture, clip, canRecord} = this.#recording;
    const busy = !!capture && capture !== 'idle';
    const explain = !this.#element('recording-consent').hidden;
    const primary = explain ? null : busy ? 'stop-recording' : canStop ? 'stop' : clip && phase !== 'error' ? 'play-recording' : 'play';
    this.#element('play').disabled = !canPlay;
    this.#element('stop').disabled = !canStop;
    this.#element('play-alternative').disabled = busy || !(canPlay || canStop);
    for (const [id, visible] of Object.entries({
      stop: !busy && !!canStop,
      'stop-recording': busy,
      'play-recording': !busy && !!clip && phase !== 'self',
      play: !busy && (!canStop || phase === 'self'),
      record: !busy && !!canRecord
    })) {
      const button = this.#element(id);
      button.hidden = !visible;
      button.classList.toggle('primary', id === primary);
      if (focused === button && (!visible || button.disabled)) this.#displacedAction = button;
    }
    if (!this.#displacedAction || !primary) return;
    const next = this.#element(primary);
    if (next.hidden || next.disabled) return;
    queueMicrotask(() => {
      if (!this.#displacedAction) return;
      const focused = this.#document.activeElement;
      if (focused !== this.#document.body && focused !== this.#displacedAction) {
        this.#displacedAction = null;
      } else if (!next.hidden && !next.disabled && next.classList.contains('primary')) {
        this.#displacedAction = null;
        next.focus({preventScroll:true});
      }
    });
  }

  setPlayback({ phase = 'idle', canPlay = false, canStop = false, message = '' }) {
    this.#playback = {phase, canPlay, canStop};
    this.#element('play-label').textContent = phase === 'error' ? '重试试听' : '听示范';
    this.#element('stop').textContent = ({self:'停止回听', loading:'取消试听'})[phase] || '停止示范';
    this.#feedback('player-message', message);
    this.#syncControls();
  }
  setOptions({ rate, repeats, round }) {
    this.#element('speed').value = String(rate);
    this.#element('repeat').value = String(repeats);
    if (round !== undefined) this.#feedback('loop-status', round && repeats > 1 ? `第 ${round} / ${repeats} 遍` : '');
    this.#element('settings-value').textContent = [rate === 1 ? '' : '0.75×', repeats > 1 ? '循环三遍' : ''].filter(Boolean).join(' · ');
    this.#element('speed-note').hidden = rate === 1;
  }

  setBookmark({ savedId, course, canMark, marked, message }) {
    this.#element('mark-bookmark').disabled = !canMark || marked;
    this.#element('mark-bookmark').textContent = marked ? '已标记本句' : '下次练这句';
    this.#element('clear-bookmark').hidden = !savedId;
    this.#element('clear-bookmark').disabled = !savedId;
    const index = course?.phrases.findIndex(phrase => phrase.id === savedId) ?? -1;
    this.#feedback('bookmark-message', message || (index >= 0 ? `下次从第 ${index + 1} 句继续` : ''));
    this.#element('phrase-list').querySelectorAll('.phrase-button').forEach(button => {
      button.querySelector('.bookmark-word').hidden = button.dataset.phraseId !== savedId;
    });
  }

  setRecording({ phase, clip, canRecord, message, elapsed = 0 }) {
    const focused = this.#document.activeElement;
    const busy = phase !== 'idle';
    this.#recording = {phase, clip, canRecord};
    this.#element('record').disabled = !canRecord || busy;
    this.#element('record-label').textContent = clip ? '再录一次' : '录这一句';
    this.#element('stop-recording').disabled = !['requesting', 'recording'].includes(phase);
    this.#element('stop-recording').textContent = ({requesting:'取消录音', saving:'正在结束…'})[phase] || '结束录音';
    this.#element('play-recording').disabled = !clip || busy;
    this.#element('delete-recording').disabled = !clip || busy;
    this.#element('delete-recording').hidden = !clip || busy;
    this.#element('recording-meta').hidden = !clip || busy;
    this.#element('capture-status').hidden = !busy;
    const state = ({ requesting: '等待麦克风', recording: '录音中', saving: '正在结束' })[phase] || '';
    if (this.#element('recording-state').textContent !== state) this.#element('recording-state').textContent = state;
    this.#element('recording-state').classList.toggle('is-recording', phase === 'recording');
    this.#element('recording-clock').textContent = phase === 'recording' ? formatSeconds(elapsed) : '';
    this.#element('recording-take').textContent = clip ? `本句第 ${clip.take} 遍 · ${formatSeconds(clip.durationMs)}` : '';
    this.#feedback('recording-message', message || '');
    this.#syncControls(focused);
  }
  showUnloaded() {
    this.#element('lesson-title').textContent = '班级作业';
    this.#element('credit').hidden = true;
  }

  resetProgress() {
    const playhead = this.#element('score-playhead');
    playhead.hidden = true;
    playhead.style.left = '0%';
    this.#element('playback-stage').textContent = '';
    this.#element('score-current').textContent = '0.0 秒';
  }

  updateProgress(phrase, { positionMs, ratio }, completed = false) {
    const percent = `${ratio * 100}%`;
    const playhead = this.#element('score-playhead');
    playhead.hidden = false;
    playhead.style.left = percent;
    const elapsed = `${((positionMs - phrase.lead_ms) / 1000).toFixed(1)} 秒`;
    const current = this.#element('score-current');
    if (current.textContent !== elapsed) current.textContent = elapsed;
    const boundary = phrase.entry_ms ?? phrase.start_ms;
    const stage = completed ? '本句已播完' : positionMs < boundary ? '前导伴奏' : phrase.entry_ms === null ? '示范唱句' : '已到开口位置';
    if (this.#element('playback-stage').textContent !== stage) this.#element('playback-stage').textContent = stage;
  }

  showWaveform(key, peaks) {
    const canvas = this.#element('score-waveform');
    const context = canvas.getContext('2d');
    if (!context) return;
    const {width, height} = canvas;
    const spacing = width / peaks.length;
    context.clearRect(0, 0, width, height);
    context.fillStyle = '#e9bf8a';
    peaks.forEach((peak, index) => {
      const size = Math.max(1.5, Math.sqrt(peak) * (height - 9));
      context.fillRect(index * spacing + spacing * .17, (height - size) / 2, spacing * .66, size);
    });
    this.#waveformKey = key;
    canvas.hidden = false;
    this.#element('score-label').textContent = '示范波形';
  }

  render(session) {
    const course = session.course;
    this.#element('lesson-title').textContent = course.title;
    this.#document.title = `${course.title} · 戏韵学伴`;
    const source = `示范 · ${course.credit}`;
    this.#element('credit').textContent = source;
    this.#element('credit').hidden = false;
    this.#element('source-credit').textContent = source;
    this.#element('class-home').hidden = !course.class_course;
    this.#element('studio-home').hidden = course.class_course || !new URLSearchParams(this.#window.location?.search).has('course');
    this.#element('phrase-list').replaceChildren();
    this.#element('workspace').hidden = !session.current;
    const questions = this.#element('course-questions');
    if (questions) questions.hidden = !course.class_course;
    if (!session.current) {
      this.showNotice('老师还没有添加可练的句子', '请联系老师确认作业内容。');
      return;
    }
    for (const [index, phrase] of course.phrases.entries()) {
      const number = this.#node('span', 'phrase-number', String(index + 1).padStart(2, '0'));
      const text = this.#node('span', 'phrase-text', phrase.text);
      const selected = this.#node('span', 'selected-word', '当前句');
      const saved = this.#node('span', 'bookmark-word', '下次练'); saved.hidden = true;
      const title = this.#node('span', 'phrase-title', text, selected, saved);
      const duration = this.#node('span', 'phrase-length', `${((phrase.end_ms - phrase.lead_ms) / 1000).toFixed(1)}s`);
      const button = this.#node('button', 'phrase-button', number, title, duration);
      button.type = 'button'; button.dataset.phraseId = phrase.id;
      this.#element('phrase-list').append(this.#node('li', null, button));
    }
    this.renderSelection(session);
  }

  renderSelection(session) {
    const current = session.current;
    this.#element('recording-consent').hidden = true;
    this.#element('phrase-list').querySelectorAll('.phrase-button').forEach(button => {
      const active = button.dataset.phraseId === current.id;
      button.setAttribute('aria-pressed', String(active));
      button.querySelector('.selected-word').hidden = !active;
    });
    const index = `第 ${session.index + 1} / ${session.course.phrases.length} 句`;
    this.#element('current-index').textContent = index;
    this.#element('phrase-count').textContent = index;
    this.#neighbors = [session.course.phrases[session.index - 1], session.course.phrases[session.index + 1]];
    this.#element('previous-phrase').disabled = !this.#neighbors[0];
    this.#element('next-phrase').disabled = !this.#neighbors[1];
    this.#element('current-text').textContent = current.text;
    this.#element('phrase-guidance').textContent = current.hint || '';
    this.#element('phrase-guidance').hidden = !current.hint;
    const comparison = this.#element('comparison');
    if (comparison) comparison.hidden = !current.alternate_course_id;
    const role = this.#element('phrase-role');
    if (role) {
      role.hidden = !current.role;
      role.textContent = current.role || '';
    }
    const note = this.#element('phrase-note');
    if (note) {
      const noteId = JSON.stringify([current.id, current.term, current.definition, current.source]);
      if (this.#noteId !== noteId) note.open = false;
      this.#noteId = noteId;
      note.hidden = !(current.term && current.definition && current.source);
      this.#element('phrase-term').textContent = current.term || '';
      this.#element('phrase-definition').textContent = current.definition || '';
      this.#element('phrase-source').textContent = current.source ? `出处 · ${current.source}` : '';
    }
    const boundary = current.entry_ms ?? current.start_ms;
    const position = `${(boundary - current.lead_ms) / (current.end_ms - current.lead_ms) * 100}%`;
    if (this.#waveformKey !== session.key()) {
      this.#element('score-waveform').hidden = true;
      this.#element('score-label').textContent = '本句时间轴';
    }
    this.#element('score-duration').textContent = formatSeconds(current.end_ms - current.lead_ms);
    this.#element('score-boundary').textContent = current.entry_ms === null ? '开口位置未标注'
      : `开口 ${formatSeconds(current.entry_ms - current.lead_ms)}`;
    this.#element('score-lead').style.width = position;
    const marker = this.#element('score-entry');
    marker.hidden = current.entry_ms === null;
    marker.style.left = position;
  }
}
