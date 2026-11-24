let opened;

function database() {
  if (!opened) {
    opened = new Promise((resolve, reject) => {
      const request = indexedDB.open('xiyun-personal', 1);
      request.onupgradeneeded = () => request.result.createObjectStore('projects', {keyPath:'id'});
      request.onsuccess = () => {
        const db = request.result;
        db.onversionchange = () => { db.close(); opened = null; };
        resolve(db);
      };
      request.onerror = () => reject(request.error);
    }).catch(error => { opened = null; throw error; });
  }
  return opened;
}

async function request(mode, action) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('projects', mode);
    const operation = action(tx.objectStore('projects'));
    tx.oncomplete = () => resolve(operation.result);
    tx.onabort = () => reject(tx.error || operation.error || new Error('本机保存被取消。'));
    tx.onerror = () => reject(tx.error || operation.error);
  });
}

export const getProject = id => request('readonly', store => store.get(id));
export const getProjects = () => request('readonly', store => store.getAll());
export const eraseProject = id => request('readwrite', store => store.delete(id));

export async function saveProject(project) {
  const db = await database();
  return new Promise((resolve, reject) => {
    const tx = db.transaction('projects', 'readwrite');
    const store = tx.objectStore('projects');
    let version, conflict;
    const read = store.get(project.id);
    read.onsuccess = () => {
      if ((read.result?.version ?? null) !== project.version) {
        conflict = new Error('这个练习已在另一页面修改或删除。请重新打开项目，或另建练习。');
        tx.abort();
        return;
      }
      version = (project.version ?? 0) + 1;
      store.put({...project, version});
    };
    tx.oncomplete = () => resolve(version);
    tx.onabort = () => reject(conflict || tx.error || new Error('本机保存被取消。'));
  });
}

export function trackRecord(track, includeMedia) {
  if (!track) return null;
  const {id, name, source, duration, start, end, fingerprint, phrase} = track;
  return {id, name, source, duration, start, end, fingerprint, phrase:includeMedia ? phrase : undefined,
    blob:includeMedia ? track.blob : undefined};
}
