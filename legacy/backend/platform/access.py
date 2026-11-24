"""本机账号、会话和班级边界。"""

import hashlib
import hmac
import re
import secrets
import time
import uuid
from http.cookies import CookieError, SimpleCookie

from ..course import CourseError, invalid, now_utc, require_text, utc_expiry


ROLES = ("admin", "teacher", "reviewer", "student")
COOKIE = "xiyun_session"
SESSION_AGE = 8 * 60 * 60


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise invalid("id", "账号编号须为 1–64 位英文字母、数字、下划线或连字符")
    return value


def password_hash(password, salt):
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)


def create_account(store, account_id, name, role, password):
    account_id = identifier(account_id)
    require_text({"name": name}, "name")
    if len(name) > 100 or role not in ROLES or not 10 <= len(password) <= 200:
        raise invalid(None, "请填写有效角色、100 字以内的名称和至少 10 位的密码")
    salt = secrets.token_bytes(16)
    digest = password_hash(password, salt)
    with store.connect(write=True) as db:
        if db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
            raise CourseError(409, "ACCOUNT_EXISTS", "该账号已存在")
        db.execute("INSERT INTO accounts VALUES (?, ?, ?, ?, ?)", (account_id, name, role, salt, digest))


def login(store, data):
    account_id = identifier(data.get("account_id"))
    password = require_text(data, "password")
    if len(password) > 200:
        raise invalid("password", "密码长度无效")
    with store.connect() as db:
        row = db.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
    salt = row["salt"] if row else bytes(16)
    digest = password_hash(password, salt)
    if not row or not hmac.compare_digest(digest, row["password"]):
        raise CourseError(401, "LOGIN_FAILED", "账号或密码不正确")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with store.connect(write=True) as db:
        db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
        db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (token_hash(token), account_id, now + SESSION_AGE))
    return token, {key: row[key] for key in ("id", "name", "role")}


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def session_cookie(token="", max_age=0):
    return f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age={max_age}"


def read_token(header):
    try:
        item = SimpleCookie(header).get(COOKIE)
    except CookieError:
        return ""
    value = item.value if item else ""
    return value if re.fullmatch(r"[A-Za-z0-9_-]{43}", value) else ""


def account_for(db, token):
    if not token:
        return None
    return db.execute(
        """SELECT accounts.id, name, role FROM accounts JOIN sessions ON accounts.id=account_id
           WHERE token_hash=? AND expires>?""",
        (token_hash(token), int(time.time())),
    ).fetchone()


def role_required(account, *roles):
    if account is None:
        raise CourseError(401, "LOGIN_REQUIRED", "请先登录工作台")
    if roles and account["role"] not in roles:
        raise CourseError(403, "ROLE_FORBIDDEN", "当前账号无权执行此操作")
    return account


def class_required(db, account, class_id):
    role_required(account)
    row = db.execute("SELECT * FROM classes WHERE id=?", (class_id,)).fetchone()
    if row is None:
        raise CourseError(404, "CLASS_UNKNOWN", "找不到班级")
    if account["role"] != "admin" and not db.execute(
        """SELECT 1 FROM class_members WHERE class_id=? AND account_id=?
           AND (expires_at IS NULL OR expires_at>?)""",
        (class_id, account["id"], now_utc()),
    ).fetchone():
        raise CourseError(403, "CLASS_FORBIDDEN", "当前账号无权访问此班级")
    return row


def course_access(db, account, course_id):
    row = db.execute("SELECT class_id FROM class_courses WHERE course_id=?", (course_id,)).fetchone()
    if row:
        class_required(db, account, row["class_id"])
    return row


def list_classes(db, account):
    if account["role"] == "admin":
        rows = db.execute("SELECT * FROM classes ORDER BY title, id")
    else:
        rows = db.execute(
            """SELECT id, title FROM classes JOIN class_members ON id=class_id WHERE account_id=?
               AND (expires_at IS NULL OR expires_at>?) ORDER BY title, id""",
            (account["id"], now_utc()),
        )
    return [dict(row) for row in rows]


def create_class(db, account, data):
    role_required(account, "admin")
    title = require_text(data, "title").strip()
    if len(title) > 100:
        raise invalid("title", "班级名称至多 100 字")
    class_id = uuid.uuid4().hex
    db.execute("INSERT INTO classes VALUES (?, ?)", (class_id, title))
    return {"id": class_id, "title": title}


def manage_member(db, account, class_id, account_id, data=None):
    role_required(account, "admin")
    class_required(db, account, class_id)
    if not db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone():
        raise CourseError(404, "ACCOUNT_UNKNOWN", "请先创建此账号")
    if data is None:
        db.execute("DELETE FROM class_members WHERE class_id=? AND account_id=?", (class_id, account_id))
    else:
        expires_at = utc_expiry(data) if data.get("expires_at") is not None else None
        if expires_at and expires_at <= now_utc():
            raise invalid("expires_at", "成员到期时间须晚于当前时间")
        db.execute(
            """INSERT INTO class_members VALUES (?, ?, ?) ON CONFLICT(class_id, account_id)
               DO UPDATE SET expires_at=excluded.expires_at""",
            (class_id, account_id, expires_at),
        )
    return {"account_id": account_id, "class_id": class_id, "enrolled": data is not None}
