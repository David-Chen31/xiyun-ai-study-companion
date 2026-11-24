"""按班课同步待练与完成事件，仅保存课项元数据。"""

import json
import uuid

from ..course import CourseError, invalid, now_utc, practice_path, require_text
from .access import class_required, course_access, role_required


def published(store, db, account, course_id):
    if not course_access(db, account, course_id):
        raise CourseError(404, "CLASS_COURSE_UNKNOWN", "找不到指定班级作业")
    return store.active_course(db, course_id)


def generation(db, account_id, course_id):
    row = db.execute("SELECT generation FROM progress_epochs WHERE account_id=? AND course_id=?",
                     (account_id, course_id)).fetchone()
    return row["generation"] if row else 0


def progress_state(db, account, course_id):
    row = db.execute("SELECT phrase_id FROM practice_bookmarks WHERE account_id=? AND course_id=?",
                     (account["id"], course_id)).fetchone()
    return {"account_id": account["id"], "generation": generation(db, account["id"], course_id),
            "phrase_id": row["phrase_id"] if row else None}


def save_event(store, db, account, data):
    role_required(account, "student")
    if set(data) != {"course_id", "revision_id", "phrase_id", "event_id", "action", "generation"}:
        raise invalid(None, "练习事件只能提交课项、动作和去重标识")
    course_id = require_text(data, "course_id")
    phrase_id = require_text(data, "phrase_id")
    action = data.get("action")
    event_id = require_text(data, "event_id")
    try:
        event_id = str(uuid.UUID(event_id))
    except ValueError as error:
        raise invalid("event_id", "事件编号须为 UUID") from error
    row = published(store, db, account, course_id)
    snapshot = json.loads(row["manifest"])
    if snapshot["revision_id"] != data["revision_id"] or not any(
        phrase["id"] == phrase_id for phrase in snapshot["phrases"]
    ):
        raise invalid("phrase_id", "课项不属于该作业修订")
    if action not in ("complete", "mark", "clear"):
        raise invalid("action", "未知的练习动作")
    if type(data["generation"]) is not int or data["generation"] != generation(db, account["id"], course_id):
        raise CourseError(409, "PROGRESS_RESET", "进度已在另一处清除，请刷新后继续")
    previous = db.execute(
        """SELECT course_id, phrase_id, action FROM practice_events WHERE account_id=? AND event_id=?""",
        (account["id"], event_id),
    ).fetchone()
    if previous:
        if tuple(previous) != (course_id, phrase_id, action):
            raise CourseError(409, "EVENT_CONFLICT", "该事件编号已用于另一项操作")
        return progress_state(db, account, course_id)
    db.execute("INSERT INTO practice_events VALUES (?, ?, ?, ?, ?, ?)",
               (account["id"], event_id, course_id, phrase_id, action, now_utc()))
    if action == "mark":
        db.execute(
            """INSERT INTO practice_bookmarks VALUES (?, ?, ?) ON CONFLICT(account_id, course_id)
               DO UPDATE SET phrase_id=excluded.phrase_id""",
            (account["id"], course_id, phrase_id),
        )
    elif action == "clear":
        db.execute("DELETE FROM practice_bookmarks WHERE account_id=? AND course_id=?", (account["id"], course_id))
    return progress_state(db, account, course_id)


def clear_progress(db, account, course_id):
    role_required(account, "student")
    if not db.execute("SELECT 1 FROM class_courses WHERE course_id=?", (course_id,)).fetchone():
        raise CourseError(404, "CLASS_COURSE_UNKNOWN", "找不到指定班级作业")
    keys = account["id"], course_id
    db.execute("DELETE FROM practice_events WHERE account_id=? AND course_id=?", keys)
    db.execute("DELETE FROM practice_bookmarks WHERE account_id=? AND course_id=?", keys)
    db.execute(
        """INSERT INTO progress_epochs VALUES (?, ?, 1) ON CONFLICT(account_id, course_id)
           DO UPDATE SET generation=generation+1""", keys,
    )
    return progress_state(db, account, course_id)


def catalog(store, db, account, classes):
    result = []
    for group in classes:
        rows = db.execute(
            """SELECT course_id FROM class_courses WHERE class_id=? ORDER BY rowid DESC""", (group["id"],),
        )
        for entry in rows:
            row = store.find_course(db, entry["course_id"])
            if store.status(row) != "published":
                continue
            manifest = json.loads(row["manifest"])
            item = {key: manifest[key] for key in ("course_id", "title", "credit")}
            item.update(class_id=group["id"], class_title=group["title"], path=practice_path(row["course_id"]))
            if account["role"] == "student":
                item["progress"] = progress_state(db, account, row["course_id"])
            result.append(item)
    return result


def class_summary(store, db, account, class_id):
    role_required(account, "teacher", "admin")
    group = class_required(db, account, class_id)
    students = [
        row["id"] for row in db.execute(
            """SELECT id FROM class_members JOIN accounts ON id=account_id WHERE class_id=? AND role='student'
               AND (expires_at IS NULL OR expires_at>?)""", (class_id, now_utc()),
        )
    ]
    student_ids = set(students)
    result = []
    for item in catalog(store, db, account, [dict(group)]):
        course = json.loads(store.find_course(db, item["course_id"])["manifest"])
        completed = {}
        marked = {}
        for counts, sql in (
            (completed, "SELECT DISTINCT phrase_id, account_id FROM practice_events WHERE course_id=? AND action='complete'"),
            (marked, "SELECT phrase_id, account_id FROM practice_bookmarks WHERE course_id=?"),
        ):
            for row in db.execute(sql, (item["course_id"],)):
                if row["account_id"] in student_ids:
                    counts[row["phrase_id"]] = counts.get(row["phrase_id"], 0) + 1
        reported = db.execute("SELECT DISTINCT account_id FROM practice_events WHERE course_id=?",
                              (item["course_id"],)).fetchall()
        result.append(item | {"received": any(row["account_id"] in student_ids for row in reported),
                              "phrases": [{"id": phrase["id"], "text": phrase["text"],
                                           "completed": completed.get(phrase["id"], 0),
                                           "marked": marked.get(phrase["id"], 0)}
                                          for phrase in course["phrases"]]})
    return {"class_id": class_id, "student_count": len(students), "period": "自作业发布以来", "courses": result}
