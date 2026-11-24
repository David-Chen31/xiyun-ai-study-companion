const root = document.getElementById('workspace');
const message = document.getElementById('message');
const identity = document.getElementById('identity');
const logout = document.getElementById('logout');
const navigation = document.getElementById('workspace-nav');
const title = document.getElementById('workspace-title');
const roleNames = {admin: '管理员', teacher: '教师', reviewer: '审核员', student: '学员'};
const stateNames = {draft: '草稿', submitted: '待审核', returned: '已退回', published: '已发布'};
const desktopEditor = window.matchMedia('(min-width: 900px)');
let workspace = null;
let selected = null;
let preview = null;
let videoPreview = null;
let mediaJobTimer = null;
let area = null;
let studioStep = 'media';
let studioPhrase = null;
let unsavedChanges = false;
let draftFilter = '';
const returnCourse = new URLSearchParams(location.search).get('return_course');

function element(tag, properties = {}, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, properties);
  node.append(...children);
  return node;
}

function report(text, error = false) {
  message.textContent = text;
  message.hidden = !text;
  message.dataset.error = String(error);
}

async function request(path, {method = 'GET', data, file, version} = {}) {
  const headers = {};
  let body;
  if (file) {
    headers['Content-Type'] = 'application/octet-stream';
    headers['If-Match'] = String(version);
    headers.Prefer = 'respond-async';
    body = file;
  } else if (data) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(data);
  }
  const response = await fetch(path, {method, headers, body, cache: 'no-store'});
  const result = await response.json();
  if (!response.ok) {
    const error = new Error(result.message || '请求未完成，请重试。');
    error.status = response.status;
    throw error;
  }
  return result;
}

function control(name, label, props = {}) {
  return element('label', {}, label, element('input', {name, required: true, ...props}));
}

function choice(name, label, options) {
  return element('label', {}, label, element('select', {name, required: true}, ...options.map(
    ([value, text]) => element('option', {value}, text)
  )));
}

function form(kind, ...children) {
  const node = element('form', {}, ...children);
  node.dataset.form = kind;
  return node;
}

function action(label, run, className = '') {
  const node = element('button', {type:'button', className}, label);
  node.addEventListener('click', () => perform(node, run));
  return node;
}

function panel(title, ...content) {
  return element('section', {className: 'panel'}, element('h2', {}, title), ...content);
}

function submit(label, className = '') {
  return element('button', {type:'submit', className}, label);
}

function options(list, label) {
  return list.map(item => [item.id, label(item)]);
}

async function perform(control, run) {
  control.disabled = true;
  report('正在处理…');
  try {
    const result = await run();
    report(result ?? '已完成。');
  } catch (error) {
    report(error.message, true);
  } finally {
    control.disabled = false;
  }
}

async function reload(nextDraft = selected?.id) {
  try {
    workspace = await request('/v1/workspace');
  } catch (error) {
    if (error.status !== 401) throw error;
    workspace = null;
  }
  showDraft(workspace?.drafts.find(item => item.id === nextDraft) || null);
}

function showDraft(draft) {
  if (draft?.id !== selected?.id) {
    studioStep = draft?.duration_ms ? 'phrases' : 'media';
    studioPhrase = null;
  }
  selected = draft;
  render();
}

function canLeaveEditor() {
  return !unsavedChanges || confirm('还有未保存的标注。确定放弃这些修改？');
}

function goToArea(next) {
  if (!canLeaveEditor()) return;
  area = next;
  showDraft(null);
  report('');
  title.focus();
}

function returnToCourse() {
  if (!workspace || !/^[A-Za-z0-9_-]{1,100}$/.test(returnCourse || '')) return false;
  location.replace(`/?course=${encodeURIComponent(returnCourse)}`);
  return true;
}

function adminPanel(permissions = false) {
  const groups = options(workspace.classes, item => item.title);
  const accounts = options(workspace.accounts, item => `${item.name}（${roleNames[item.role]} · ${item.id}）`);
  const members = workspace.members.map(member => element('div', {className:'item'},
    element('p', {}, `${groups.find(item => item[0] === member.class_id)?.[1]} · ${member.account_id}`),
    action('移出班级', async () => {
      await request(`/v1/classes/${member.class_id}/members/${member.account_id}`, {method:'DELETE'});
      await reload();
    })
  ));
  const grants = workspace.grants.map(grant => element('article', {className:'item'},
    element('p', {}, `${grant.grant_ref} · ${grant.revoked_at ? '已撤权' : `到期 ${grant.expires_at}`}`),
    ...(!grant.revoked_at ? [form('revoke-grant',
      element('input', {type:'hidden', name:'grant_ref', value:grant.grant_ref}),
      control('reason', '撤销许可原因'), submit('撤销许可'))] : [])
  ));
  if (permissions) return element('div', {className:'task-area'},
    panel('登记许可', form('grant', control('file', '许可台账 JSON', {type:'file', accept:'.json'}), submit('核对并登记', 'primary'))),
    panel('已登记许可', element('div', {className:'stack'}, ...grants)));
  return element('div', {className:'task-area'},
    element('div', {className:'grid'},
      panel('创建班级', form('class', control('title', '班级名称', {maxLength:100}), submit('创建班级'))),
      panel('创建账号', form('account', control('account_id', '账号编号'),
        control('name', '显示名称'), choice('role', '角色', Object.entries(roleNames)),
        control('password', '初始密码', {type:'password', minLength:10, autocomplete:'new-password'}), submit('创建账号'))),
      panel('添加成员', groups.length && accounts.length ? form('membership',
        choice('class_id', '班级', groups), choice('account_id', '账号', accounts),
        control('expires_at', '到期 UTC 时间（可选）', {required:false, placeholder:'2027-01-01T00:00:00Z'}),
        submit('添加成员')) : element('p', {}, '先创建班级和账号。'))
    ),
    element('details', {className:'panel'}, element('summary', {}, '已登记班级成员'),
      element('div', {className:'stack'}, ...members))
  );
}

function coursePanel() {
  const student = workspace.account.role === 'student';
  const courses = [...workspace.courses].sort((a, b) => Number(!!b.progress?.phrase_id) - Number(!!a.progress?.phrase_id));
  const items = courses.map((course, index) => {
    const resume = !!course.progress?.phrase_id;
    const manage = student ? element('details', {className:'record-actions'}, element('summary', {}, '练习记录'),
      action('清除进度', async () => {
        if (!confirm('清除此作业的待练和完成记录？')) return '已取消。';
        await request(`/v1/me/progress?course_id=${course.course_id}`, {method:'DELETE'});
        await reload();
      }, 'quiet danger')) : null;
    return element('article', {className:`course-row${index === 0 && resume ? ' course-resume' : ''}`},
      element('div', {className:'row-copy'}, element('h2', {}, course.title),
        element('p', {className:'muted'}, [course.class_title, course.credit].filter(Boolean).join(' · ')),
        ...(resume ? [element('p', {className:'subtle'}, '接着上次标记的句子练')] : []), ...(manage ? [manage] : [])),
      element('a', {href:course.path, className:index === 0 ? 'button primary' : 'button'}, resume ? '继续练习' : '打开作业'));
  });
  return element('div', {className:'task-list'}, ...(items.length ? items : [element('p', {className:'empty-state'}, '当前班级暂无可用作业。') ]));
}

function draftPanel() {
  const teacher = workspace.account.role === 'teacher';
  const order = teacher ? ['returned','draft','submitted','published'] : ['submitted','returned','published','draft'];
  const all = [...workspace.drafts].sort((a, b) => order.indexOf(a.state) - order.indexOf(b.state));
  const items = all.filter(item => !draftFilter || item.state === draftFilter).map(item => element('article', {className:'course-row'},
    element('div', {className:'row-copy'}, element('h2', {}, item.content.title),
      element('p', {className:'muted'}, classTitle(item)),
      stateLabel(item.state, item), ...(item.feedback ? [element('p', {className:'subtle'}, item.feedback)] : [])),
    action(teacher && ['draft','returned'].includes(item.state) ? '继续制课' : item.state === 'submitted' && !teacher ? '开始审核' : '查看作业',
      async () => {
        const draft = await request(`/v1/drafts/${item.id}`);
        if (!content.isConnected) return '';
        showDraft(draft);
        title.focus();
        return '';
      })
  ));
  const create = teacher && workspace.classes.length ? element('details', {className:'new-assignment'},
    element('summary', {className:'button primary'}, '新建作业'), form('new-draft',
    choice('class_id', '投放班级', options(workspace.classes, item => item.title)),
    control('title', '作业名称', {maxLength:100}), control('grant_ref', '许可编号'), submit('创建草稿'))) : null;
  const filters = choice('draft_state', '作业状态', [['','全部'], ...Object.entries(stateNames)]);
  filters.className = 'list-filter';
  const select = filters.querySelector('select');
  select.value = draftFilter;
  select.addEventListener('change', () => { draftFilter = select.value; render(); root.querySelector('.list-filter select').focus(); });
  const content = element('div', {className:'task-area'},
    element('div', {className:'list-toolbar'}, filters, ...(create ? [create] : [])),
    element('div', {className:'task-list'}, ...(items.length ? items : [element('p', {className:'empty-state'},
      teacher && !workspace.classes.length ? '加入班级后即可新建作业。' : teacher ? '这里还没有符合条件的作业。' : '当前没有符合条件的审核作业。')])));
  return content;
}

function classTitle(draft) {
  return workspace.classes.find(group => group.id === draft.class_id)?.title || '所属班级';
}

function activePublication(draft) {
  return workspace.courses.some(course => course.course_id === draft.course_id);
}

function stateLabel(state, draft) {
  const closed = state === 'published' && !activePublication(draft);
  const label = element('span', {className:'state-label'}, closed ? '访问已关闭' : stateNames[state]);
  label.dataset.state = closed ? 'closed' : state;
  return label;
}

function pauseDraftMedia() {
  for (const player of [preview, videoPreview?.video]) {
    if (!player) continue;
    player.pause();
    delete player.dataset.end;
  }
}

function playWindow(lead, end) {
  if (!preview || !Number.isFinite(lead) || !Number.isFinite(end) || lead < 0 || end <= lead) {
    throw new Error('请填写有效前导和句末。');
  }
  const followVideo = videoPreview?.follow.checked;
  const target = followVideo ? videoPreview.video : preview;
  if (followVideo) preview.pause();
  else videoPreview?.video.pause();
  const origin = followVideo ? videoPreview.audioStart : 0;
  target.currentTime = Math.max(0, lead / 1000 + origin);
  target.dataset.end = end / 1000 + origin;
  return target.play();
}

function enforcePreviewWindow(media) {
  media.addEventListener('timeupdate', () => {
    if (media.dataset.end && media.currentTime >= Number(media.dataset.end)) {
      media.pause();
      delete media.dataset.end;
    }
  });
}

function videoPanel(metadata) {
  const video = element('video', {src:metadata.stream_url, controls:true, preload:'metadata', playsInline:true});
  const follow = element('input', {type:'checkbox'});
  const label = element('label', {className:'inline-option'}, follow, '试听乐句时同步定位到视频');
  const state = {video, follow, audioStart:metadata.audio_start_ms / 1000, timer:null};
  videoPreview = state;
  enforcePreviewWindow(video);
  const cancelCheck = () => clearTimeout(state.timer);
  async function check() {
    if (videoPreview !== state || video.paused) return;
    try {
      const response = await fetch(metadata.stream_url, {method:'HEAD', cache:'no-store', signal:AbortSignal.timeout(2500)});
      if (!response.ok) throw new Error('视频预览权限已失效。');
      if (videoPreview === state && !video.paused) state.timer = setTimeout(check, 2500);
    } catch {
      if (videoPreview !== state || video.paused) return;
      video.pause();
      video.removeAttribute('src');
      video.load();
      follow.checked = false;
      follow.disabled = true;
      report('视频预览已停止，请重新打开草稿核对访问权限。', true);
    }
  }
  video.addEventListener('play', () => { preview?.pause(); cancelCheck(); void check(); });
  ['pause', 'ended', 'emptied'].forEach(name => video.addEventListener(name, cancelCheck));
  return element('details', {}, element('summary', {}, '授权视频预览'), video, label);
}

function comparison(phrase) {
  const courses = workspace.courses.filter(course => course.class_id === selected.class_id);
  const courseLabel = choice('alternate_course_id', '对照课包', [['', '不设置'], ...courses.map(course => [course.course_id, course.title])]);
  const phraseLabel = choice('alternate_phrase_id', '对应句', [['', '这个版本缺句']]);
  const course = courseLabel.querySelector('select'), target = phraseLabel.querySelector('select');
  course.required = target.required = false;
  if (phrase.alternate_course_id && !courses.some(item => item.course_id === phrase.alternate_course_id)) {
    course.add(element('option', {value:phrase.alternate_course_id}, '原对照课包已失效，请重新选择'));
    target.add(element('option', {value:phrase.alternate_phrase_id}, '原对应句'));
  }
  course.value = phrase.alternate_course_id || '';
  target.value = phrase.alternate_phrase_id || '';
  let version = 0;
  const load = async (initial = '') => {
    const requestVersion = ++version;
    const courseId = course.value;
    target.replaceChildren(element('option', {value:''}, '这个版本缺句'));
    if (!courseId || !courses.some(item => item.course_id === courseId)) return;
    target.disabled = true;
    try {
      const draft = await request(`/v1/courses/${courseId}`);
      if (version !== requestVersion) return;
      draft.phrases.forEach((item, index) => target.add(element('option', {value:item.id}, `${index + 1}. ${item.text}`)));
      target.value = initial || '';
    } finally {
      if (version === requestVersion) target.disabled = false;
    }
  };
  course.addEventListener('change', () => {
    target.disabled = false;
    load().catch(error => report(error.message, true));
  });
  if (course.value && courses.some(item => item.course_id === course.value)) {
    load(phrase.alternate_phrase_id).catch(error => report(error.message, true));
  }
  return element('details', {}, element('summary', {}, '同句版本对照'), courseLabel, phraseLabel);
}

function phraseItem(phrase, canEdit, list) {
  const item = element('article', {className:'phrase'});
  item.dataset.id = phrase.id;
  if (canEdit) {
    item.append(element('label', {}, '实唱词', element('textarea', {name:'text', rows:3, required:true, value:phrase.text || ''})),
      element('div', {className:'time-fields'},
        ...[['lead_ms','前导'], ['start_ms','句首'], ['entry_ms','入点（可空）'], ['end_ms','句末']].map(([key, label]) =>
          control(key, label + ' ms', {type:'number', min:0, max:selected.duration_ms, value:phrase[key] ?? '', required:key !== 'entry_ms'})
        )
      ),
      element('details', {}, element('summary', {}, '教学补充'),
        control('role', '角色（可选）', {required:false, value:phrase.role || ''}),
        control('hint', '听辨提示（可选）', {required:false, value:phrase.hint || ''})),
      element('details', {}, element('summary', {}, '句旁知识卡'),
        control('term', '术语', {required:false, value:phrase.term || ''}),
        control('definition', '释义', {required:false, value:phrase.definition || ''}),
        control('source', '出处（书名、页码或网址）', {required:false, value:phrase.source || ''})
      ),
      comparison(phrase),
      element('div', {className:'actions'}, action('试听本句', async () => {
        await playWindow(item.querySelector('[name=lead_ms]').valueAsNumber, item.querySelector('[name=end_ms]').valueAsNumber);
        return '正在试听本句。';
      })),
      element('details', {className:'inline-disclosure'}, element('summary', {}, '句序与移除'),
        element('div', {className:'actions'}, action('上移', () => {
          if (item.previousElementSibling) list.insertBefore(item, item.previousElementSibling);
          list.dispatchEvent(new Event('input', {bubbles:true}));
          return '已调整句序，保存后生效。';
        }),
        action('下移', () => {
          if (item.nextElementSibling) list.insertBefore(item.nextElementSibling, item);
          list.dispatchEvent(new Event('input', {bubbles:true}));
          return '已调整句序，保存后生效。';
        }),
        action('移除本句', () => {
          item.remove();
          list.dispatchEvent(new Event('input', {bubbles:true}));
          return '已移除本句，保存后生效。';
        }, 'quiet danger'))
      )
    );
  } else {
    item.append(element('p', {className:'subtle'}, `前导 ${phrase.lead_ms} · 句首 ${phrase.start_ms} · 入点 ${phrase.entry_ms ?? '未标注'} · 句末 ${phrase.end_ms} ms`),
      ...(phrase.role ? [element('p', {}, '角色 · ' + phrase.role)] : []),
      ...(phrase.term ? [element('p', {}, phrase.term + ' · ' + phrase.definition), element('p', {className:'subtle'}, '出处 · ' + phrase.source)] : []),
      ...(phrase.alternate_course_id ? [
        element('a', {href:`/?course=${encodeURIComponent(phrase.alternate_course_id)}`, target:'_blank', rel:'noopener'}, '核对另一版本')
      ] : [])
    );
  }
  return item;
}

function waveformEditor(draft, editForm) {
  const canvas = element('canvas', {width:900, height:164});
  canvas.setAttribute('role', 'img');
  canvas.setAttribute('aria-label', '示范振幅、当前句范围及播放头。乐句字段支持键盘输入。');
  const context = canvas.getContext('2d');
  const status = element('p', {className:'subtle', role:'status'});
  if (!context) return status;
  const labels = {lead_ms:'前导', start_ms:'句首', entry_ms:'入点', end_ms:'句末'};
  const colors = {lead_ms:'#d8c08b', start_ms:'#a2dfbc', entry_ms:'#ffc58e', end_ms:'#9fb9e1'};
  const ruler = element('div', {className:'waveform-ticks'});
  const player = preview;
  const video = videoPreview;
  let item = null;
  let active = null;
  let zoomed = false;

  const read = name => item?.querySelector('[name=' + name + ']')?.valueAsNumber;
  const span = () => {
    const start = read('lead_ms'), end = read('end_ms');
    if (!zoomed || !Number.isFinite(start) || !Number.isFinite(end) || start >= end) return [0, draft.duration_ms];
    const margin = (end - start) * .12;
    const left = Math.max(0, start - margin);
    const right = Math.min(draft.duration_ms, end + margin);
    return right > left ? [left, right] : [0, draft.duration_ms];
  };
  const zoom = element('button', {type:'button', className:'quiet'}, '放大本句');
  zoom.addEventListener('click', () => {
    zoomed = !zoomed;
    zoom.textContent = zoomed ? '查看全段' : '放大本句';
    zoom.setAttribute('aria-pressed', String(zoomed));
    draw();
  });

  const direction = editForm ? element('div', {className:'marker-tools'}) : null;
  const choices = [];
  if (direction) for (const [name, label] of Object.entries(labels)) {
    const button = element('button', {type:'button', className:'quiet marker-tool'}, label);
    button.style.setProperty('--marker-color', colors[name]);
    button.dataset.marker = name;
    button.addEventListener('click', () => {
      const field = item?.querySelector('[name=' + name + ']');
      if (!field) return;
      selectMarker(field);
      status.textContent = '点击波形设置' + label + '，或直接输入毫秒。';
    });
    direction.append(button);
    choices.push(button);
  }
  function selectMarker(field) {
    active = field;
    canvas.dataset.editable = 'true';
    draw();
  }

  function draw() {
    const {width, height} = canvas;
    const [start, end] = span(), scale = width / (end - start);
    const position = time => (time - start) * scale;
    context.fillStyle = '#13251e';
    context.fillRect(0, 0, width, height);
    context.strokeStyle = '#385147';
    context.lineWidth = 1;
    context.beginPath();
    for (let i = 1; i < 8; i++) {
      const x = i * width / 8;
      context.moveTo(x, 0);
      context.lineTo(x, height);
    }
    context.moveTo(0, height / 2);
    context.lineTo(width, height / 2);
    context.stroke();
    const first = read('lead_ms'), last = read('end_ms'), entry = read('entry_ms');
    const onset = Number.isFinite(entry) ? entry : read('start_ms');
    if (Number.isFinite(first) && Number.isFinite(last) && last > first) {
      context.fillStyle = '#244438';
      context.fillRect(position(first), 0, (last - first) * scale, height);
      if (Number.isFinite(onset) && onset > first && onset <= last) {
        context.fillStyle = '#584d35';
        context.fillRect(position(first), 0, (onset - first) * scale, height);
      }
    }
    const bucket = draft.duration_ms / draft.waveform.length;
    const spacing = Math.max(2, bucket * scale * .6);
    draft.waveform.forEach((value, index) => {
      const time = (index + .5) * bucket;
      if (time < start || time > end) return;
      const size = Math.max(2, Math.sqrt(value) * (height - 18));
      context.fillStyle = time >= first && time <= last ? '#cbddca' : '#799687';
      context.fillRect(position(time) - spacing / 2, (height - size) / 2, spacing, size);
    });
    for (const [name, color] of Object.entries(colors)) {
      const time = read(name);
      if (!Number.isFinite(time) || time < start || time > end) continue;
      context.fillStyle = color;
      context.fillRect(Math.min(width - 2, position(time)), 0, active?.name === name ? 3 : 1.5, height);
    }
    const seconds = video && !video.video.paused ? video.video.currentTime - video.audioStart : player?.currentTime;
    const cursor = position(seconds * 1000);
    if (Number.isFinite(cursor) && cursor >= 0 && cursor <= width) {
      context.fillStyle = '#fff9ee';
      context.fillRect(Math.min(width - 2, cursor), 0, 2, height);
    }
    zoom.disabled = !item || !Number.isFinite(first) || !Number.isFinite(last) || last <= first;
    for (const button of choices) {
      button.disabled = !item;
      button.setAttribute('aria-pressed', String(button.dataset.marker === active?.name));
    }
    const times = [start, (start + end) / 2, end].map(ms => (ms / 1000).toFixed(2) + 's');
    if (ruler.dataset.ticks !== times.join(',')) {
      ruler.dataset.ticks = times.join(',');
      ruler.replaceChildren(...times.map(time => element('span', {}, time)));
    }
  }

  player?.addEventListener('timeupdate', draw);
  video?.video.addEventListener('timeupdate', draw);
  if (editForm) {
    editForm.addEventListener('phrase-selection', event => {
      item = event.detail;
      active = null;
      delete canvas.dataset.editable;
      draw();
    });
    editForm.addEventListener('focusin', event => {
      if (Object.hasOwn(labels, event.target.name)) selectMarker(event.target);
    });
    editForm.addEventListener('input', event => {
      if (Object.hasOwn(labels, event.target.name)) draw();
    });
    let dragRange = null;
    const locate = event => {
      if (!active?.isConnected || !dragRange) return;
      const bounds = canvas.getBoundingClientRect();
      const fraction = Math.max(0, Math.min(1, (event.clientX - bounds.left) / bounds.width));
      const [start, end] = dragRange;
      active.value = Math.round(start + fraction * (end - start));
      active.dispatchEvent(new Event('input', {bubbles:true}));
      status.textContent = labels[active.name] + ' · ' + active.value + ' ms，保存后生效。';
    };
    canvas.addEventListener('pointerdown', event => {
      if (!active) return;
      dragRange = span();
      canvas.setPointerCapture(event.pointerId);
      locate(event);
    });
    canvas.addEventListener('pointermove', event => {
      if (canvas.hasPointerCapture(event.pointerId)) locate(event);
    });
    canvas.addEventListener('lostpointercapture', () => { dragRange = null; });
  }
  draw();
  const header = element('div', {className:'waveform-header'}, element('span', {}, '示范波形'), ...(editForm ? [zoom] : []));
  return element('div', {className:'waveform'}, header, canvas, ruler,
    ...(direction ? [direction] : []), status);
}

function mediaJobStatus(draft, upload, update) {
  const status = element('p', {className:'muted'});
  status.setAttribute('role', 'status');
  const button = upload.querySelector('button');
  async function refresh() {
    if (!status.isConnected) return;
    try {
      const {job} = await request(`/v1/drafts/${draft.id}/media-job`);
      if (!status.isConnected) return;
      button.disabled = job?.state === 'processing';
      update(button.disabled);
      if (job?.state === 'processing') {
        status.textContent = '正在检查或转换素材。可以离开草稿，稍后回来查看。';
        mediaJobTimer = setTimeout(refresh, 700);
      } else if (job?.state === 'ready') {
        if (draft.version <= job.draft_version && !unsavedChanges) { await reload(draft.id); return; }
        status.textContent = '素材已处理，可试听和校订。';
      } else {
        status.textContent = job?.error?.message || '';
      }
    } catch {
      if (status.isConnected) {
        status.textContent = '暂时无法读取处理状态，重新打开草稿可再查。';
        button.disabled = false;
      }
    }
  }
  queueMicrotask(refresh);
  return status;
}

function transcriptionPanel(draft, addCandidate) {
  const bounds = {type:'number', min:0, max:draft.duration_ms, step:1};
  const editor = element('form', {hidden:true},
    element('div', {className:'grid'},
      control('start_ms', '起点 ms', {...bounds, value:0}),
      control('end_ms', '终点 ms', {...bounds, value:Math.min(28000, draft.duration_ms)})),
    element('label', {}, '参考唱词（可选）', element('textarea', {name:'hint', maxLength:120, rows:2})),
    element('p', {className:'muted'}, '京剧清唱容易识别错字。有词本时可粘贴本段唱词，生成后请逐字试听并核对时间。'),
    submit('运行本机转写'));
  const status = element('p', {className:'muted'});
  status.setAttribute('role', 'status');
  const output = element('div', {className:'stack'});
  const panel = element('details', {}, element('summary', {}, '唱词转写候选'), status, editor, output);
  const button = editor.querySelector('button');
  let timer = null;
  async function refresh() {
    if (!panel.isConnected || !panel.open) return;
    try {
      const result = await request(`/v1/drafts/${draft.id}/transcription`);
      if (!panel.isConnected || !panel.open) return;
      const {configured, permitted, job} = result;
      editor.hidden = !configured || !permitted;
      button.disabled = job?.state === 'processing';
      if (!permitted) status.textContent = '本草稿的素材许可须包含唱词转写用途。';
      else if (!configured) status.textContent = '本机转写模型未配置，可继续手填。';
      else if (job?.state === 'processing') {
        status.textContent = '正在本机识别，可以稍后回来查看。';
        timer = setTimeout(refresh, 1000);
      } else if (job?.error) status.textContent = job.error.message;
      else if (job?.state === 'ready') {
        status.textContent = job.candidates.length
          ? '请核对唱词和时间。加入句表后仍须保存并送审。'
          : '此窗口没有可用候选，可换一个范围或手填。';
      } else status.textContent = '选择 0.5–28 秒的示范窗口，生成待校订的候选。';
      output.replaceChildren(...(job?.candidates || []).map(candidate =>
        element('div', {className:'item'}, element('p', {}, candidate.text),
          element('p', {className:'muted'}, `${candidate.start_ms}–${candidate.end_ms} ms`),
          element('div', {className:'actions'},
            action('试听转写窗口', async () => { await playWindow(candidate.start_ms, candidate.end_ms); return '正在试听候选。'; }),
            action('将唱词候选加入句表', () => {
              addCandidate({...candidate, lead_ms:candidate.start_ms});
              return '候选已放入编辑区，请校订后保存。';
            })
          )
        )));
    } catch (error) {
      if (panel.isConnected) {
        status.textContent = error.message;
        editor.hidden = true;
        button.disabled = false;
        output.replaceChildren();
      }
    }
  }
  panel.addEventListener('toggle', () => {
    clearTimeout(timer);
    if (panel.open) void refresh();
  });
  editor.addEventListener('submit', async event => {
    event.preventDefault();
    event.stopPropagation();
    button.disabled = true;
    output.replaceChildren();
    status.textContent = '正在提交…';
    const data = Object.fromEntries(new FormData(editor));
    try {
      await request(`/v1/drafts/${draft.id}/transcription`, {method:'POST',
        data:{start_ms:Number(data.start_ms),end_ms:Number(data.end_ms),hint:data.hint,version:draft.version}});
      await refresh();
    } catch (error) {
      status.textContent = error.message;
      button.disabled = false;
    }
  });
  return panel;
}

function draftMedia(draft) {
  const media = element('div', {className:'draft-media'});
  if (!draft.stream_url) return media;
  const audio = element('audio', {src:draft.stream_url, controls:true, preload:'metadata'});
  audio.setAttribute('aria-label', '示范音轨');
  enforcePreviewWindow(audio);
  audio.addEventListener('play', () => videoPreview?.video.pause());
  audio.addEventListener('error', () => {
    if (preview === audio) report('音轨暂不可用，请核对许可或重新登录。', true);
  });
  preview = audio;
  media.append(audio);
  if (draft.video_preview) media.append(videoPanel(draft.video_preview));
  if (draft.source_type === 'mp4') {
    media.append(element('p', {className:'subtle'},
      '源视频音轨相对画面偏移 ' + draft.audio_offset_ms + ' ms。句表以音轨零点为准。'));
  }
  return media;
}

function draftPreview(draft, reviewing) {
  const card = element('div', {className:'practice-card work-preview'});
  const body = element('div', {className:'stack'});
  const phrases = draft.content.phrases;
  const summary = element('p', {className:'muted'}, classTitle(draft) + ' · 示范 ' + draft.content.credit);
  card.append(element('h2', {}, reviewing ? '审核预览' : '学员视角预览'), summary);
  if (!phrases.length) {
    card.append(element('p', {className:'empty-state'}, '还没有已保存的乐句。'));
    return card;
  }
  const selector = choice('preview_phrase', '试听乐句', phrases.map((phrase, index) => [phrase.id, '第 ' + (index + 1) + ' 句 · ' + phrase.text]));
  const select = selector.querySelector('select');
  select.required = false;
  select.value = phrases.find(phrase => phrase.id === studioPhrase)?.id || phrases[0].id;
  const show = () => {
    pauseDraftMedia();
    const index = phrases.findIndex(phrase => phrase.id === select.value);
    const phrase = phrases[index];
    const heading = element('h3', {className:'current-lyric'}, phrase.text);
    const listen = action('听示范', async () => { await playWindow(phrase.lead_ms, phrase.end_ms); return ''; });
    const elements = [element('p', {className:'phrase-index'}, '第 ' + (index + 1) + ' / ' + phrases.length + ' 句'),
      heading, ...(phrase.hint ? [element('p', {className:'phrase-guidance'}, phrase.hint)] : []),
      element('div', {className:'actions'}, listen)];
    if (reviewing) elements.push(phraseItem(phrase, false));
    body.replaceChildren(...elements);
  };
  select.addEventListener('change', show);
  show();
  card.append(selector, body);
  return card;
}

function reviewActions(draft) {
  const publish = form('review',
    element('input', {type:'hidden', name:'decision', value:'publish'}),
    submit('确认发布', 'primary'));
  const returnDraft = element('details', {className:'inline-disclosure'},
    element('summary', {}, '退回修改'),
    form('review', element('input', {type:'hidden', name:'decision', value:'return'}),
      element('label', {}, '退回理由', element('textarea', {name:'reason', rows:3, required:true})), submit('退回给老师')));
  return panel('审核决定',
    element('p', {}, '发布班级 · ' + classTitle(draft)),
    element('p', {className:'muted'}, '许可编号 ' + draft.grant_ref + ' · 有效至 ' + draft.content.expires_at),
    element('div', {className:'release-actions'}, publish, returnDraft));
}

function publishedActions(draft, canReview) {
  const active = activePublication(draft);
  const link = '/?course=' + encodeURIComponent(draft.course_id);
  return panel(active ? '已发布作业' : '作业访问已关闭',
    element('p', {className:'muted'}, classTitle(draft)),
    element('div', {className:'actions'}, ...(active ? [element('a', {href:link, className:'button'}, '打开学员作业'),
      action('复制班级链接', async () => {
        await navigator.clipboard.writeText(new URL(link, location.origin).href);
        return '班级作业链接已复制。';
      })] : []),
      ...(active && workspace.account.role === 'teacher' ? [action('复制为新草稿', async () => {
        const next = await request('/v1/drafts/' + draft.id + '/fork', {method:'POST', data:{version:draft.version}});
        await reload(next.id);
        return '新草稿已创建。';
      })] : [])),
    ...(canReview && active ? [
      element('details', {className:'inline-disclosure'}, element('summary', {}, '撤回作业'),
        form('withdraw', control('reason', '撤回原因'), submit('撤回此作业', 'danger')))
    ] : []));
}

function draftEditor() {
  const draft = selected;
  const canEdit = workspace.account.role === 'teacher' && ['draft', 'returned'].includes(draft.state);
  const canReview = ['reviewer', 'admin'].includes(workspace.account.role);
  const section = element('article', {className:'draft-studio'});
  const summary = element('div', {className:'draft-summary'}, stateLabel(draft.state, draft),
    element('span', {className:'muted'}, classTitle(draft)),
    element('span', {className:'muted'}, '版本 ' + draft.version));
  section.append(summary);
  if (draft.feedback) section.append(element('p', {className:'callout'}, '退回原因 · ' + draft.feedback));
  const mediaPlayer = draftMedia(draft);
  if (!canEdit) {
    section.append(draftPreview(draft, canReview));
    if (draft.duration_ms) section.append(panel('素材试听', mediaPlayer));
    if (canReview && draft.state === 'submitted') section.append(reviewActions(draft));
    if (draft.course_id) section.append(publishedActions(draft, canReview));
    return section;
  }

  const steps = element('nav', {className:'studio-steps'});
  steps.setAttribute('aria-label', '制课步骤');
  const saveStatus = element('span', {className:'muted'}, '已保存');
  saveStatus.setAttribute('role', 'status');
  summary.append(saveStatus);

  const sections = {};
  const stepButtons = {};
  const playerSlots = {};
  let busyMedia = false;
  let updateControls;
  const stepNames = {media:'素材', phrases:'乐句标注', preview:'预览送审'};
  for (const [key, label] of Object.entries(stepNames)) {
    const button = element('button', {type:'button', className:'quiet step-button'}, label);
    button.addEventListener('click', () => go(key, true));
    button.setAttribute('aria-controls', 'studio-' + key);
    stepButtons[key] = button;
    steps.append(button);
    sections[key] = element('section', {id:'studio-' + key, className:'studio-step', hidden:true});
    playerSlots[key] = element('div', {className:'player-slot', hidden:!draft.duration_ms});
  }
  section.append(steps, ...Object.values(sections));
  function go(step, moveFocus = false) {
    if (stepButtons[step].disabled) return;
    studioStep = step;
    pauseDraftMedia();
    playerSlots[step].append(mediaPlayer);
    if (step === 'preview') {
      const picker = sections.preview.querySelector('select');
      if (picker && [...picker.options].some(option => option.value === studioPhrase)) {
        picker.value = studioPhrase;
        picker.dispatchEvent(new Event('change'));
      }
    }
    for (const key of Object.keys(sections)) {
      sections[key].hidden = key !== step;
      if (key === step) stepButtons[key].setAttribute('aria-current', 'step');
      else stepButtons[key].removeAttribute('aria-current');
    }
    if (moveFocus) sections[step].querySelector('h2')?.focus();
    report('');
  }

  const media = sections.media;
  const upload = form('upload',
    control('file', '选择获授权的 WAV、MP3 或 MP4',
      {type:'file', accept:'.wav,.mp3,.mp4'}), submit(draft.duration_ms ? '提交替换' : '上传并检查', draft.duration_ms ? '' : 'primary'));
  const mediaTitle = element('h2', {tabIndex:-1}, '示范素材');
  const mediaStatus = mediaJobStatus(draft, upload, busy => {
    busyMedia = busy;
    updateControls();
    if (busy && studioStep !== 'media' && !unsavedChanges) go('media');
  });
  const begin = element('button', {type:'button', className:'primary', hidden:!draft.duration_ms}, '开始标注');
  begin.addEventListener('click', () => go('phrases', true));
  media.append(element('div', {className:'panel content-panel'}, mediaTitle,
    element('p', {className:'muted'}, '许可 ' + draft.grant_ref + ' · ' + draft.content.credit),
    ...(draft.duration_ms ? [element('p', {className:'subtle'}, '已载入 ' + draft.source_type.toUpperCase()
      + ' · ' + (draft.duration_ms / 1000).toFixed(1) + ' 秒')] : []),
    playerSlots.media, ...(draft.duration_ms ? [element('div', {className:'actions'}, begin)] : []),
    draft.duration_ms ? element('details', {className:'inline-disclosure'}, element('summary', {}, '替换示范素材'), upload) : upload,
    mediaStatus));

  const phrases = sections.phrases;
  const editTitle = element('h2', {tabIndex:-1}, '校订乐句');
  const list = element('div', {className:'phrase-fields'});
  const selector = element('div', {className:'sentence-list'});
  const pickerSummary = element('summary');
  const sentencePicker = element('details', {className:'panel editor-index', open:desktopEditor.matches}, pickerSummary, selector);
  const empty = element('p', {className:'empty-state'}, '添加第一句，填写实唱词和时间。');
  const add = element('button', {type:'button'}, '添加一句');
  const save = submit('保存句表', 'primary');
  const next = element('button', {type:'button', className:'primary'}, '预览作业');
  next.addEventListener('click', () => go('preview', true));

  const editor = form('edit',
    list, empty,
    element('details', {className:'inline-disclosure assignment-settings'}, element('summary', {}, '作业设置'),
      control('title', '作业名称', {value:draft.content.title, maxLength:100}),
      control('expires_at', '到期时间（UTC）', {value:draft.content.expires_at})));
  editor.id = 'phrase-editor';
  save.setAttribute('form', editor.id);
  editor.noValidate = true;
  list.append(...draft.content.phrases.map(phrase => phraseItem(phrase, true, list)));

  function items() { return [...list.querySelectorAll('.phrase')]; }
  function refreshPhrases(resetWaveform = true) {
    const entries = items();
    studioPhrase = entries.find(item => item.dataset.id === studioPhrase)?.dataset.id || entries[0]?.dataset.id || null;
    const index = entries.findIndex(item => item.dataset.id === studioPhrase);
    pickerSummary.textContent = entries.length ? `选句 · 第 ${index + 1} / ${entries.length} 句` : '选句 · 尚未添加';
    selector.replaceChildren(...entries.map((item, index) => {
      const active = item.dataset.id === studioPhrase;
      item.hidden = !active;
      const text = item.querySelector('[name=text]').value.trim();
      const button = element('button', {type:'button', className:'quiet sentence-option'},
        element('span', {className:'subtle'}, String(index + 1).padStart(2, '0')),
        element('span', {}, text || '未填写唱词'));
      button.setAttribute('aria-pressed', String(active));
      button.addEventListener('click', () => {
        if (!active) {
          pauseDraftMedia();
          studioPhrase = item.dataset.id;
          refreshPhrases();
        }
        if (!desktopEditor.matches) sentencePicker.open = false;
        item.querySelector('[name=text]').focus();
      });
      return button;
    }));
    empty.hidden = !!entries.length;
    add.classList.toggle('primary', !entries.length);
    if (resetWaveform) {
      editor.dispatchEvent(new CustomEvent('phrase-selection', {detail:entries.find(item => !item.hidden)}));
    }
  }

  function addPhrase(values) {
    const item = phraseItem({id:crypto.randomUUID(), ...values}, true, list);
    list.append(item);
    studioPhrase = item.dataset.id;
    refreshPhrases();
    unsavedChanges = true;
    updateControls();
    item.querySelector('[name=text]').focus();
  }
  add.addEventListener('click', () => addPhrase({start_ms:0, lead_ms:0}));
  editor.addEventListener('input', event => {
    unsavedChanges = true;
    if (event.target === list || event.target.name === 'text') refreshPhrases(event.target === list);
    updateControls();
  });
  editor.addEventListener('reveal-invalid', event => {
    const field = event.detail;
    const item = field.closest('.phrase');
    if (item && item.hidden) {
      studioPhrase = item.dataset.id;
      refreshPhrases();
    }
    for (let parent = field.parentElement; parent && parent !== editor; parent = parent.parentElement) {
      if (parent.tagName === 'DETAILS') parent.open = true;
    }
    field.reportValidity();
    field.focus();
  });

  const tools = [];
  if (draft.duration_ms) {
    tools.push(transcriptionPanel(draft, addPhrase));
    if (draft.activity_suggestions?.length) tools.push(element('details', {className:'inline-disclosure'},
      element('summary', {}, '音量活动候选'), element('p', {className:'subtle'}, '按示范振幅估计，请逐段试听确认。'),
      ...draft.activity_suggestions.map(({start_ms, end_ms}) => element('div', {className:'actions'},
        element('span', {}, start_ms + '–' + end_ms + ' ms'),
        action('试听', async () => { await playWindow(start_ms, end_ms); return ''; }),
        action('放入句表', () => { addPhrase({lead_ms:start_ms, start_ms, end_ms}); return '请补唱词并校订时间。'; })))));
  }
  const wave = draft.waveform ? waveformEditor(draft, editor) : element('p', {className:'subtle'}, '暂无波形，可以直接输入时间。');
  phrases.append(element('div', {className:'step-heading'}, editTitle, element('div', {className:'actions'}, save, next)),
    element('div', {className:'editor-grid'},
      element('div', {className:'editor-phrases'}, sentencePicker, add),
      element('section', {className:'panel editor-audio'}, element('h3', {}, '示范音轨'), playerSlots.phrases, wave,
        ...(tools.length ? [element('details', {className:'inline-disclosure'}, element('summary', {}, '辅助拆句'), ...tools)] : [])),
      element('section', {className:'panel editor-fields'}, element('h3', {}, '当前句'), editor)));

  const publishArea = sections.preview;
  publishArea.append(draftPreview(draft, false), playerSlots.preview, panel('送交独立审核',
    element('p', {className:'muted'}, classTitle(draft) + ' · 许可 ' + draft.grant_ref + ' · 有效至 ' + draft.content.expires_at),
    action('送交审核', async () => {
      if (unsavedChanges) throw new Error('请先保存修改，再送交审核。');
      if (!confirm('将这份作业送交独立审核？发布班级为「' + classTitle(draft) + '」。')) return '已取消。';
      await request('/v1/drafts/' + draft.id + '/submit', {method:'POST', data:{version:draft.version}});
      await reload();
      return '已送交审核。';
    }, 'primary')));

  updateControls = () => {
    const hasSavedPhrases = !!draft.content.phrases.length;
    const hasEditablePhrases = !!items().length;
    saveStatus.textContent = unsavedChanges ? '有未保存修改' : '已保存';
    save.hidden = !unsavedChanges || !hasEditablePhrases && !hasSavedPhrases;
    next.hidden = unsavedChanges || !hasSavedPhrases;
    stepButtons.phrases.disabled = !draft.duration_ms || busyMedia;
    stepButtons.preview.disabled = busyMedia || unsavedChanges || !hasSavedPhrases;
    begin.disabled = busyMedia;
  };
  refreshPhrases();
  updateControls();
  go(stepButtons[studioStep].disabled ? draft.duration_ms ? 'phrases' : 'media' : studioStep);
  return section;
}

function summaryPanel() {
  if (!workspace.classes.length) return element('p', {className:'empty-state'}, '加入班级后可查看练习摘要。');
  const area = element('div', {className:'stack'});
  return panel('按句练习摘要',
    element('div', {className:'actions'}, ...workspace.classes.map(group => action(group.title, async () => {
      const summary = await request(`/v1/classes/${group.id}/summary`);
      area.replaceChildren(element('p', {}, `${summary.period} · 在班学员 ${summary.student_count} 人`),
        ...summary.courses.map(course => element('article', {className:'item summary-meters'}, element('h3', {}, course.title),
          ...(course.received ? course.phrases.map(phrase => element('article', {}, element('p', {}, phrase.text),
            ...[['完成', phrase.completed], ['待练', phrase.marked]].map(([name, count]) => {
              const meter = element('meter', {min:0, max:summary.student_count, value:count});
              meter.setAttribute('aria-label', phrase.text + ' · ' + name);
              return element('div', {className:'meter-line'}, element('span', {}, name + ' ' + count + ' 人'), meter);
            }))) : [element('p', {}, '尚无练习记录。')])
        ))
      );
      return '';
    }))), area);
}

function render() {
  unsavedChanges = false;
  clearTimeout(mediaJobTimer);
  if (videoPreview) {
    const {video, timer} = videoPreview;
    videoPreview = null;
    clearTimeout(timer);
    video.pause();
    video.removeAttribute('src');
  }
  if (preview) {
    preview.pause();
    preview.removeAttribute('src');
    preview = null;
  }
  logout.hidden = !workspace;
  identity.textContent = workspace ? `${workspace.account.name} · ${roleNames[workspace.account.role]}` : '';
  if (!workspace) {
    area = null;
    draftFilter = '';
    navigation.hidden = true;
    title.textContent = '班级登录';
    root.replaceChildren(document.getElementById('login').content.cloneNode(true));
    return;
  }
  const role = workspace.account.role;
  const roles = {
    student: [['courses','班级作业',coursePanel]],
    teacher: [['drafts','我的作业',draftPanel], ['courses','已发作业',coursePanel], ['summary','班级摘要',summaryPanel]],
    reviewer: [['drafts','审核队列',draftPanel], ['courses','已发作业',coursePanel]],
    admin: [['members','班级与成员',adminPanel], ['permissions','素材许可',() => adminPanel(true)],
      ['drafts','审核队列',draftPanel], ['courses','已发作业',coursePanel], ['summary','班级摘要',summaryPanel]]
  };
  const areas = roles[role];
  const [activeArea, heading, content] = areas.find(([key]) => key === area) || areas[0];
  area = activeArea;
  navigation.hidden = areas.length === 1;
  navigation.replaceChildren(...areas.map(([key, label]) => {
    const button = element('button', {type:'button', className:'quiet'}, label);
    if (key === area) button.setAttribute('aria-current', 'page');
    button.addEventListener('click', () => goToArea(key));
    return button;
  }));
  title.textContent = selected ? selected.content.title : heading;
  document.title = title.textContent + ' · 戏韵学伴';
  if (selected) {
    const back = element('button', {type:'button', className:'quiet back-button'}, '‹ 返回' + heading);
    back.addEventListener('click', () => goToArea(activeArea));
    const studio = draftEditor();
    studio.querySelector('.draft-summary').prepend(back);
    root.replaceChildren(studio);
  } else root.replaceChildren(content());
}

async function submitForm(node) {
  const data = Object.fromEntries(new FormData(node));
  const kind = node.dataset.form;
  const draft = selected;
  const base = draft ? `/v1/drafts/${draft.id}` : '';
  switch (kind) {
    case 'login':
      await request('/v1/sessions', {method:'POST', data});
      await reload();
      if (!returnToCourse()) title.focus();
      return '';
    case 'class': await request('/v1/classes', {method:'POST', data}); break;
    case 'account': await request('/v1/accounts', {method:'POST', data}); break;
    case 'membership':
      await request(`/v1/classes/${data.class_id}/members/${data.account_id}`, {method:'PUT', data:{expires_at:data.expires_at || null}});
      break;
    case 'grant': {
      const text = (await data.file.text()).replace(/^\uFEFF/, '');
      await request('/v1/grants', {method:'POST', data:JSON.parse(text)});
      break;
    }
    case 'revoke-grant':
      if (!confirm('撤销后，该许可关联的课程和草稿都将失去播放权限。确认撤销？')) return '已取消。';
      await request(`/v1/grants/${encodeURIComponent(data.grant_ref)}/revoke`, {method:'POST', data:{reason:data.reason}});
      break;
    case 'new-draft': {
      const result = await request('/v1/drafts', {method:'POST', data});
      await reload(node.isConnected ? result.id : selected?.id);
      title.focus();
      return '草稿已创建，可上传音频。';
    }
    case 'upload': {
      if (unsavedChanges) throw new Error('请先保存乐句再替换素材。');
      if (draft.content.phrases.length && !confirm('替换示范后请重新试听并核对 ' + draft.content.phrases.length + ' 句的时间。继续？')) return '已取消。';
      await request(base + '/media', {method:'PUT', file:data.file, version:draft.version});
      if (node.isConnected) await reload(draft.id);
      return '文件已提交处理，状态显示在上传区。';
    }
    case 'edit': {
      const invalid = [...node.elements].find(field => field.willValidate && !field.validity.valid);
      if (invalid) {
        node.dispatchEvent(new CustomEvent('reveal-invalid', {detail:invalid}));
        return '请填写标出的字段。';
      }
      if (node.querySelector('[name=alternate_phrase_id]:disabled')) throw new Error('请等对照句加载完成后再保存。');
      const phrases = [...node.querySelectorAll('.phrase')].map(item => {
        const read = name => item.querySelector(`[name=${name}]`).value;
        return {id:item.dataset.id, text:read('text'), role:read('role'), hint:read('hint'),
          term:read('term'), definition:read('definition'), source:read('source'),
          alternate_course_id:read('alternate_course_id'), alternate_phrase_id:read('alternate_phrase_id'),
          lead_ms:Number(read('lead_ms')), start_ms:Number(read('start_ms')),
          end_ms:Number(read('end_ms')), entry_ms:read('entry_ms') === '' ? null : Number(read('entry_ms'))};
      });
      const step = node.closest('.studio-step');
      step.inert = true;
      try {
        await request(base, {method:'PATCH', data:{title:data.title, expires_at:data.expires_at, version:draft.version, phrases}});
        if (node.isConnected) await reload(draft.id);
      } catch (error) {
        if (error.status === 409) throw new Error(error.message + ' 当前输入仍保留，可复制后重新打开草稿。');
        throw error;
      } finally { step.inert = false; }
      root.querySelector('#studio-phrases h2')?.focus();
      return '';
    }
    case 'review':
      if (data.decision === 'publish' && !confirm('将「' + draft.content.title + '」发布给「' + classTitle(draft)
        + '」？素材有效至 ' + draft.content.expires_at + '。')) return '已取消。';
      await request(base + '/review', {method:'POST', data:{...data, version:draft.version}});
      await reload();
      title.focus();
      return data.decision === 'publish' ? '作业已发布，可复制班级链接。' : '作业已退回老师。';
    case 'withdraw':
      if (!confirm('撤回后，「' + classTitle(draft) + '」的学员将无法继续访问这份作业。确认撤回？')) return '已取消。';
      await request(`/v1/courses/${draft.course_id}/withdraw`, {method:'POST', data});
      await reload();
      title.focus();
      return '作业已撤回。';
  }
  await reload();
  return kind === 'login' ? '' : '已保存。';
}

root.addEventListener('submit', event => {
  event.preventDefault();
  const node = event.target;
  void perform(event.submitter || node.querySelector('button[type=submit]'), () => submitForm(node));
});
logout.addEventListener('click', () => perform(logout, async () => {
  if (!canLeaveEditor()) return '';
  await request('/v1/sessions/current', {method:'DELETE'});
  await reload();
  return '已退出。';
}));
window.addEventListener('beforeunload', event => {
  if (!unsavedChanges) return;
  event.preventDefault();
  event.returnValue = '';
});
desktopEditor.addEventListener('change', event => {
  const picker = root.querySelector('.editor-index');
  if (picker) picker.open = event.matches;
});
reload().then(() => { if (!returnToCourse()) report(''); }).catch(error => report(error.message, true));
