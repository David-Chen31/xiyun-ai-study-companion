export class CourseError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}

// The root URL opens the sample; an explicit course link always uses A's API.
export function courseOptions(search, origin) {
  const params = new URLSearchParams(search);
  return {
    origin,
    courseId: params.get('course') || 'c1',
    demo: !params.has('course'),
    scenario: params.get('scenario') || ''
  };
}

const invalid = () => { throw new CourseError('INVALID_COURSE', '作业数据不完整，请联系老师检查后重试。'); };
const isText = (value) => typeof value === 'string' && value.trim().length > 0;
const isTime = (value) => Number.isSafeInteger(value) && value >= 0;

// Preserve the API's original millisecond coordinates and phrase order.
export function validateCourse(course, origin) {
  if (!course || !['course_id', 'revision_id', 'media_id', 'title', 'credit', 'stream_url'].every(key => isText(course[key])) || !isTime(course.duration_ms) || course.duration_ms === 0 || !Array.isArray(course.phrases)) invalid();
  let stream;
  try { stream = new URL(course.stream_url, origin); } catch { invalid(); }
  if (stream.origin !== origin || stream.username || stream.password || !['http:', 'https:'].includes(stream.protocol)) invalid();
  const ids = new Set();
  for (const phrase of course.phrases) {
    if (!phrase || !isText(phrase.id) || !isText(phrase.text) || ids.has(phrase.id)) invalid();
    ids.add(phrase.id);
    if (![phrase.lead_ms, phrase.start_ms, phrase.end_ms].every(isTime) || phrase.lead_ms > phrase.start_ms || phrase.start_ms >= phrase.end_ms || phrase.end_ms > course.duration_ms) invalid();
    if (phrase.entry_ms !== null && (!isTime(phrase.entry_ms) || phrase.entry_ms < phrase.lead_ms || phrase.entry_ms >= phrase.end_ms)) invalid();
  }
  return course;
}

export function selectedPhrase(course, previousId) {
  return course.phrases.find(phrase => phrase.id === previousId) ?? course.phrases[0] ?? null;
}

export function playbackWindow(phrase) {
  return { offset: phrase.lead_ms / 1000, duration: (phrase.end_ms - phrase.lead_ms) / 1000 };
}

export function responseError(status) {
  if (status === 401) return new CourseError('LOGIN_REQUIRED', '请进入班级工作台登录，再打开这份作业。');
  if (status === 410) return new CourseError('COURSE_GONE', '这份作业已撤回或授权已到期，请联系老师获取新的作业。');
  if (status === 404) return new CourseError('COURSE_UNKNOWN', '没有找到这份作业，请确认老师发来的链接。');
  if (status === 403) return new CourseError('MEDIA_FORBIDDEN', '暂时无法访问这份示范，请联系老师确认访问权限。');
  return new CourseError('NETWORK', '暂时无法加载，请检查网络后重试。');
}

export async function fetchResource(fetcher, path, signal) {
  let response;
  try { response = await fetcher(path, { signal, cache: 'no-store', credentials: 'same-origin' }); }
  catch (error) {
    if (error.name === 'AbortError') throw error;
    throw responseError(0);
  }
  if (!response.ok) throw responseError(response.status);
  return response;
}

export class PracticeSession {
  #origin;
  #course = null;
  #selectedId = null;

  constructor(origin) { this.#origin = origin; }
  get course() { return this.#course; }
  get current() { return this.#course ? selectedPhrase(this.#course, this.#selectedId) : null; }
  get index() { return this.#course?.phrases.findIndex(phrase => phrase.id === this.#selectedId) ?? -1; }

  key(phrase = this.current) {
    const course = this.#course;
    return phrase ? JSON.stringify([course.course_id, course.revision_id, course.media_id, phrase.id]) : null;
  }
  get keys() { return this.#course?.phrases.map(phrase => this.key(phrase)) ?? []; }

  replace(data) {
    validateCourse(data, this.#origin);
    const sameRevision = this.#course?.course_id === data.course_id && this.#course?.revision_id === data.revision_id;
    const previousId = sameRevision ? this.#selectedId : null;
    this.#course = { ...data, phrases: data.phrases.map(phrase => ({ ...phrase })) };
    this.#selectedId = selectedPhrase(this.#course, previousId)?.id ?? null;
    return !sameRevision;
  }

  select(id) {
    if (!this.#course?.phrases.some(phrase => phrase.id === id)) return false;
    this.#selectedId = id;
    return true;
  }
}

// Persist only an explicit phrase bookmark, never recordings or playback state.
export class PracticeBookmark {
  #storage;
  #scope;
  #message = '';

  constructor({ storage = () => globalThis.localStorage, scope = 'course' } = {}) {
    this.#storage = storage; this.#scope = scope;
  }
  get message() { return this.#message; }
  #key(course) { return `xiyun:practice:${JSON.stringify([this.#scope, course.course_id])}`; }
  #access(action, fallback) {
    this.#message = '';
    try { return action(this.#storage()); }
    catch { this.#message = '无法访问本地待练记录，请检查浏览器存储设置；仍可正常听练和录音。'; return fallback; }
  }

  read(course) {
    return this.#access(storage => {
      const key = this.#key(course), raw = storage.getItem(key);
      if (!raw) return null;
      let saved;
      try { saved = JSON.parse(raw); } catch { storage.removeItem(key); return null; }
      if (saved?.revision_id !== course.revision_id || !course.phrases.some(phrase => phrase.id === saved.phrase_id)) {
        storage.removeItem(key); return null;
      }
      return saved.phrase_id;
    }, null);
  }
  save(course, phraseId) {
    return this.#access(storage => {
      storage.setItem(this.#key(course), JSON.stringify({ revision_id: course.revision_id, phrase_id: phraseId }));
      return true;
    }, false);
  }
  clear(course) {
    return this.#access(storage => { storage.removeItem(this.#key(course)); return true; }, false);
  }
}

// Sources share transport/error handling; PracticeSession validates incoming data.
export class CourseRepository {
  #fetcher;

  constructor({ fetcher = globalThis.fetch }) {
    // Native browser fetch must keep its Window receiver after encapsulation.
    this.#fetcher = fetcher.bind(globalThis);
  }

  async readJson(path, signal) {
    const response = await fetchResource(this.#fetcher, path, signal);
    try { return await response.json(); }
    catch { invalid(); }
  }
}

export class ApiCourseRepository extends CourseRepository {
  #courseId;
  #origin;
  constructor(options) { super(options); this.#courseId = options.courseId; this.#origin = options.origin; }

  async load(signal) {
    const course = await this.readJson(`/v1/courses/${encodeURIComponent(this.#courseId)}`, signal);
    if (course?.course_id !== this.#courseId) invalid();
    return course;
  }

  async alternative(phraseId, signal) {
    const course = encodeURIComponent(this.#courseId), phrase = encodeURIComponent(phraseId);
    const result = await this.readJson(`/v1/courses/${course}/alternatives?phrase_id=${phrase}`, signal);
    if (result.alternative) validateCourse(result.alternative, this.#origin);
    return result;
  }
}

export class DemoCourseRepository extends CourseRepository {
  #scenario;
  constructor(options) { super(options); this.#scenario = options.scenario; }

  async load(signal) {
    if (this.#scenario === 'gone') throw responseError(410);
    if (this.#scenario === 'error') throw new CourseError('NETWORK', '暂时无法读取作业，请检查网络后重试。');
    const course = await this.readJson('./示例数据/三句作业.json', signal);
    if (this.#scenario === 'empty') course.phrases = [];
    return course;
  }
}

export function createCourseClient(options) {
  return options.demo ? new DemoCourseRepository(options) : new ApiCourseRepository(options);
}
