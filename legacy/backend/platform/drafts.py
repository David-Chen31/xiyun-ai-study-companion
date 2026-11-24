"""人工校订、独立评审和按班发布。"""

import hashlib
import json
import uuid

from ai_backend.media import MediaPreparationError, activity_windows, media_type, prepare_media, waveform

from ..course import CourseError, check_phrase_windows, invalid, json_text, normalize_course, now_utc, require_text
from ..store import Store
from .access import class_required, role_required
from .learning import accepted_mapping


def grant_for(db, grant_ref):
    row = db.execute("SELECT record FROM grants WHERE grant_ref=?", (grant_ref,)).fetchone()
    if row is None:
        raise CourseError(403, "GRANT_UNKNOWN", "操作员须先登记此许可", "grant_ref")
    if db.execute("SELECT 1 FROM grant_revocations WHERE grant_ref=?", (grant_ref,)).fetchone():
        raise CourseError(410, "COURSE_GONE", "素材许可已撤回")
    record = json.loads(row["record"])
    if record["expires_at"] <= now_utc():
        raise CourseError(410, "COURSE_GONE", "素材许可已到期")
    return record


def audit(db, account, object_id, action, reason=None):
    db.execute(
        "INSERT INTO audit_log (account_id, object_id, action, reason, happened_at) VALUES (?, ?, ?, ?, ?)",
        (account["id"], object_id, action, reason, now_utc()),
    )


def instructor(db, account, class_id):
    role_required(account, "teacher")
    class_required(db, account, class_id)


def create_draft(db, account, data):
    class_id = require_text(data, "class_id")
    instructor(db, account, class_id)
    grant_ref = require_text(data, "grant_ref")
    grant = grant_for(db, grant_ref)
    title = require_text(data, "title").strip()
    if len(title) > 100:
        raise invalid("title", "作业名称至多 100 字")
    draft_id = uuid.uuid4().hex
    content = {"title": title, "credit": grant["credit"], "expires_at": grant["expires_at"], "phrases": []}
    db.execute("INSERT INTO drafts (id, owner_id, class_id, grant_ref, content) VALUES (?, ?, ?, ?, ?)",
               (draft_id, account["id"], class_id, grant_ref, json_text(content)))
    return detail(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def detail(row):
    analysis = json.loads(row["analysis"]) if row["analysis"] else None
    preview = analysis.get("preview") if analysis else None
    return {key: row[key] for key in ("id", "owner_id", "class_id", "grant_ref", "version", "state",
                                     "feedback", "course_id")} | {
        "content": json.loads(row["content"]),
        "duration_ms": analysis["duration_ms"] if analysis else None,
        "source_type": analysis.get("source_type", "wav") if analysis else None,
        "audio_offset_ms": analysis.get("audio_offset_ms", 0) if analysis else None,
        "waveform": analysis.get("waveform") if analysis else None,
        "activity_suggestions": activity_windows(analysis.get("waveform"), analysis["duration_ms"]) if analysis else [],
        "stream_url": f"/v1/drafts/{row['id']}/media" if analysis else None,
        "video_preview": ({
            "stream_url": f"/v1/drafts/{row['id']}/video",
            "audio_start_ms": preview["audio_start_ms"],
            "video_start_ms": preview["video_start_ms"],
        } if preview else None),
    }


def find_draft(db, account, draft_id):
    role_required(account, "teacher", "reviewer", "admin")
    row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
    if row is None:
        raise CourseError(404, "DRAFT_UNKNOWN", "找不到草稿")
    class_required(db, account, row["class_id"])
    if account["role"] == "teacher" and row["owner_id"] != account["id"]:
        raise CourseError(403, "DRAFT_FORBIDDEN", "只能查看本人草稿")
    if account["role"] == "reviewer" and row["state"] == "draft":
        raise CourseError(403, "DRAFT_FORBIDDEN", "草稿送审后才可复核")
    return row


def check_version(row, version):
    if type(version) is not int or row["version"] != version:
        raise CourseError(409, "REVISION_CONFLICT", "草稿已更新，请重新加载再操作")


def editable(db, account, draft_id, version):
    role_required(account, "teacher")
    row = find_draft(db, account, draft_id)
    check_version(row, version)
    if row["state"] not in ("draft", "returned"):
        raise CourseError(409, "DRAFT_LOCKED", "请从可编辑草稿继续")
    return row


def list_drafts(db, account, classes):
    if account["role"] == "student":
        return []
    visible_classes = {item["id"] for item in classes}
    return [
        detail(row) for row in db.execute("SELECT * FROM drafts ORDER BY rowid DESC")
        if row["class_id"] in visible_classes
        and (account["role"] != "teacher" or row["owner_id"] == account["id"])
        and (account["role"] != "reviewer" or row["state"] != "draft")
    ]


def attach_audio(store, inspector, account, draft_id, version, audio, *, job=None):
    source_digest = hashlib.sha256(audio).hexdigest()
    with store.connect() as db:
        row = editable(db, account, draft_id, version)
        grant = grant_for(db, row["grant_ref"])
        if grant["audio_sha256"] != source_digest:
            raise CourseError(403, "GRANT_AUDIO_MISMATCH", "文件摘要与登记许可不一致")
    try:
        kind = media_type(audio)
        if kind == "mp4" and "video_audio_extraction" not in grant["purposes"]:
            raise CourseError(403, "VIDEO_SCOPE_REQUIRED", "许可须明确包含视频音轨提取用途")
        wav, prepared, preview = prepare_media(audio, kind, video_preview="video_preview" in grant["purposes"])
    except MediaPreparationError as error:
        status = 503 if error.code in ("CONVERTER_UNAVAILABLE", "CONVERSION_TIMEOUT") else 415
        raise CourseError(status, error.code, str(error)) from error
    digest = hashlib.sha256(wav).hexdigest()
    request = {
        "request_id": uuid.uuid4().hex,
        "revision_id": f"{draft_id}-{version}",
        "phrases": [{"id": "probe", "text": "校验窗口", "lead_ms": 0, "start_ms": 0, "end_ms": 1, "entry_ms": None}],
    }
    analysis = inspector.inspect(wav, request) | prepared | {
        "source_sha256": source_digest,
        "waveform": waveform(wav),
    }
    if preview is not None:
        analysis["preview"]["digest"] = hashlib.sha256(preview).hexdigest()
    with store.connect(write=True) as db:
        row = editable(db, account, draft_id, version)
        grant_for(db, row["grant_ref"])
        active_job = db.execute("SELECT id FROM media_jobs WHERE draft_id=? AND state='processing'",
                                (draft_id,)).fetchone()
        if (active_job["id"] if active_job else None) != job:
            raise CourseError(409, "UPLOAD_IN_PROGRESS", "处理任务已更改，请重新打开草稿")
        saved = json.loads(row["content"])
        windows(saved["phrases"], analysis["duration_ms"])
        db.execute("INSERT INTO media (digest, wav) VALUES (?, ?) ON CONFLICT (digest) DO NOTHING", (digest, wav))
        if preview is not None:
            db.execute("INSERT INTO video_previews (digest, mp4) VALUES (?, ?) ON CONFLICT (digest) DO NOTHING",
                       (analysis["preview"]["digest"], preview))
        db.execute("UPDATE drafts SET digest=?, analysis=?, version=version+1 WHERE id=?",
                   (digest, json_text(analysis), draft_id))
        if job:
            db.execute("UPDATE media_jobs SET state='ready', finished_at=? WHERE id=?", (now_utc(), job))
        return detail(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def windows(phrases, duration):
    try:
        check_phrase_windows(phrases, duration)
    except ValueError as error:
        raise CourseError(422, "INVALID_BOUNDARY", "请检查句编号及前导、句首、入点和句末是否有效", "phrases") from error


def snapshot(row, content, reviewer_id):
    return normalize_course(content | {
        "course_id": row["id"], "revision_id": f"r{row['version']}", "media_id": f"m-{row['id']}",
        "author_id": row["owner_id"], "reviewer_id": reviewer_id, "grant_ref": row["grant_ref"],
    })


def phrase_details(content, course):
    for source, phrase in zip(content["phrases"], course["phrases"]):
        for key in ("role", "hint", "term", "definition", "source", "alternate_course_id", "alternate_phrase_id"):
            value = source.get(key, "")
            if not isinstance(value, str) or len(value) > 400:
                raise invalid(key, "标注须为 400 字以内的文本")
            if value.strip():
                require_text(source, key)
            phrase[key] = value.strip()
        fields = ("term", "definition", "source")
        if any(phrase[key] for key in fields) and not all(phrase[key] for key in fields):
            raise invalid("definition", "知识卡须同时填写术语、释义和来源")
    return course["phrases"]


def check_mappings(store, db, row, phrases):
    for phrase in phrases:
        accepted_mapping(store, db, row["class_id"], row["id"], phrase["alternate_course_id"],
                         phrase["alternate_phrase_id"])


def update_draft(store, db, account, draft_id, data):
    row = editable(db, account, draft_id, data.get("version"))
    grant = grant_for(db, row["grant_ref"])
    content = data | {"credit": grant["credit"]}
    content.setdefault("expires_at", grant["expires_at"])
    candidate = snapshot(row, content, f"review-pending-{row['id']}")
    if len(candidate["title"]) > 100:
        raise invalid("title", "作业名称至多 100 字")
    if candidate["expires_at"] > grant["expires_at"] or candidate["expires_at"] <= now_utc():
        raise invalid("expires_at", "作业到期时间须在许可有效期内")
    if row["analysis"] is None:
        raise CourseError(409, "MEDIA_REQUIRED", "请先上传并检查授权 WAV")
    windows(candidate["phrases"], json.loads(row["analysis"])["duration_ms"])
    saved = {key: candidate[key] for key in ("title", "credit", "expires_at")}
    saved["phrases"] = phrase_details(content, candidate)
    check_mappings(store, db, row, saved["phrases"])
    db.execute("UPDATE drafts SET content=?, version=version+1 WHERE id=?", (json_text(saved), draft_id))
    return detail(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def submit(db, account, draft_id, data):
    row = editable(db, account, draft_id, data.get("version"))
    candidate = snapshot(row, json.loads(row["content"]), f"review-pending-{row['id']}")
    analysis = json.loads(row["analysis"])
    Store.check_grant(db, candidate, analysis.get("source_sha256", row["digest"]))
    db.execute("UPDATE drafts SET state='submitted', version=version+1, feedback=NULL WHERE id=?", (draft_id,))
    audit(db, account, draft_id, "submit")
    return detail(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def review(store, db, account, draft_id, data):
    role_required(account, "reviewer", "admin")
    row = find_draft(db, account, draft_id)
    check_version(row, data.get("version"))
    if row["owner_id"] == account["id"]:
        raise CourseError(403, "SELF_REVIEW", "请由独立账号评审")
    if row["state"] != "submitted":
        raise CourseError(409, "NOT_SUBMITTED", "仅可评审已送审的草稿")
    decision = data.get("decision")
    if decision == "return":
        reason = require_text(data, "reason").strip()
        db.execute("UPDATE drafts SET state='returned', feedback=?, version=version+1 WHERE id=?", (reason, draft_id))
        audit(db, account, draft_id, "return", reason)
    elif decision == "publish":
        content = json.loads(row["content"])
        course = snapshot(row, content, account["id"])
        course["phrases"] = phrase_details(content, course)
        check_mappings(store, db, row, course["phrases"])
        analysis = json.loads(row["analysis"])
        Store.check_grant(db, course, analysis.get("source_sha256", row["digest"]))
        windows(course["phrases"], analysis["duration_ms"])
        db.execute(
            """INSERT INTO courses (course_id, grant_ref, audio_sha256, manifest, analysis, expires_at, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (draft_id, row["grant_ref"], row["digest"], json_text(course), row["analysis"],
             course["expires_at"], now_utc()),
        )
        db.execute("INSERT INTO class_courses VALUES (?, ?)", (draft_id, row["class_id"]))
        db.execute("UPDATE drafts SET state='published', course_id=?, version=version+1 WHERE id=?",
                   (draft_id, draft_id))
        audit(db, account, draft_id, "publish")
    else:
        raise invalid("decision", "请选择发布或退回")
    return detail(db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())


def fork(db, account, draft_id, data):
    role_required(account, "teacher")
    row = find_draft(db, account, draft_id)
    check_version(row, data.get("version"))
    if row["state"] != "published":
        raise CourseError(409, "NOT_PUBLISHED", "仅可复制已发布课包")
    grant_for(db, row["grant_ref"])
    new_id = uuid.uuid4().hex
    db.execute(
        """INSERT INTO drafts (id, owner_id, class_id, grant_ref, digest, analysis, content)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (new_id, row["owner_id"], row["class_id"], row["grant_ref"], row["digest"], row["analysis"], row["content"]),
    )
    return detail(db.execute("SELECT * FROM drafts WHERE id=?", (new_id,)).fetchone())
