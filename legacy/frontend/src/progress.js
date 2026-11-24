// Only course, revision, phrase and action identifiers enter the offline queue.
export class AccountProgress {
  #window;
  #course = null;
  #pending = new Map();
  #sending = false;
  #completed = new WeakSet();

  constructor(window) { this.#window = window; }

  #prefix(course) {
    return `xiyun:outbox:${JSON.stringify([course.progress.account_id, course.course_id])}:`;
  }

  #events(course) {
    const prefix = this.#prefix(course);
    const items = new Map(this.#pending);
    try {
      const storage = this.#window.localStorage;
      for (let index = 0; index < storage.length; index++) {
        const key = storage.key(index);
        if (key?.startsWith(prefix)) {
          try { items.set(key, JSON.parse(storage.getItem(key))); } catch { /* ignore corrupt entries */ }
        }
      }
    } catch { /* the in-memory queue can still send */ }
    return [...items].filter(([key, item]) => key.startsWith(prefix) && item?.event)
      .sort((left, right) => left[1].queuedAt - right[1].queuedAt);
  }

  #remove(key) {
    this.#pending.delete(key);
    try { this.#window.localStorage.removeItem(key); } catch { /* storage may be disabled */ }
  }

  update(course) {
    this.#course = course.progress ? course : null;
    if (!this.#course) return null;
    const events = this.#events(course);
    let selected = course.progress.phrase_id;
    for (const [key, {event}] of events) {
      if (event.generation !== course.progress.generation) this.#remove(key);
      else if (event.action === 'mark') selected = event.phrase_id;
      else if (event.action === 'clear') selected = null;
    }
    void this.flush();
    return selected;
  }

  record(action, phraseId) {
    const course = this.#course;
    if (!course) return false;
    const eventId = this.#window.crypto.randomUUID();
    const event = {event_id:eventId, action, phrase_id:phraseId, course_id:course.course_id,
      revision_id:course.revision_id, generation:course.progress.generation};
    const key = this.#prefix(course) + eventId;
    const item = {event, queuedAt:Date.now()};
    this.#pending.set(key, item);
    try { this.#window.localStorage.setItem(key, JSON.stringify(item)); } catch { /* send while open */ }
    void this.flush();
    return true;
  }

  complete(clip, phraseId) {
    if (this.#course && clip && !this.#completed.has(clip) && this.record('complete', phraseId)) {
      this.#completed.add(clip);
    }
  }

  async flush() {
    const course = this.#course;
    if (!course || this.#sending) return;
    this.#sending = true;
    try {
      let next;
      while (this.#course === course && (next = this.#events(course)[0])) {
        const [key, {event}] = next;
        if (event.generation !== course.progress.generation) { this.#remove(key); continue; }
        let response;
        try {
          response = await this.#window.fetch('/v1/practice-events', {
            method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(event),
            signal:AbortSignal.timeout(8000)
          });
        } catch { return; }
        if (response.ok || [400, 403, 404, 409, 410, 422].includes(response.status)) this.#remove(key);
        else return;
      }
    } finally {
      this.#sending = false;
      if (this.#course && this.#course !== course) void this.flush();
    }
  }
}
