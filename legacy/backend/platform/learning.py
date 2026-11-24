"""在授权且已发布的修订内提供人工对照与资料查询。"""

import json
import re
import unicodedata

from ..course import CourseError, invalid, public_course, require_text
from .progress import published


def accepted_mapping(store, db, class_id, draft_id, target_id, target_phrase):
    if not target_id:
        if target_phrase:
            raise invalid("alternate_phrase_id", "请先选择对照课程")
        return
    link = db.execute("SELECT class_id FROM class_courses WHERE course_id=?", (target_id,)).fetchone()
    if target_id == draft_id or not link or link["class_id"] != class_id:
        raise invalid("alternate_course_id", "请选择同一班级内已发布的另一份作业")
    row = store.active_course(db, target_id)
    if target_phrase and not any(phrase["id"] == target_phrase for phrase in json.loads(row["manifest"])["phrases"]):
        raise invalid("alternate_phrase_id", "对照句不属于指定修订")


def alternative(store, db, account, course_id, phrase_id):
    source = published(store, db, account, course_id)
    manifest = json.loads(source["manifest"])
    phrase = next((item for item in manifest["phrases"] if item["id"] == phrase_id), None)
    if phrase is None:
        raise invalid("phrase_id", "找不到所属课项")
    target_id, target_phrase = phrase.get("alternate_course_id"), phrase.get("alternate_phrase_id")
    if not target_id or not target_phrase:
        return {"alternative": None, "reason": "该对照版本未收录本句。"}
    try:
        target = published(store, db, account, target_id)
    except CourseError as error:
        if error.status not in (403, 404, 410):
            raise
        return {"alternative": None, "reason": "对照版本当前不可用，仍可听原作业。"}
    candidate = json.loads(target["manifest"])
    target_item = next((item for item in candidate["phrases"] if item["id"] == target_phrase), None)
    if not target_item:
        return {"alternative": None, "reason": "该对照版本未收录本句。"}
    candidate["phrases"] = [target_item]
    course = public_course(candidate, json.loads(target["analysis"])["duration_ms"])
    return {"alternative": course | {"phrase_stream": True}}


def normalize_term(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text).casefold()).strip("?。！!")


def requested_terms(query, available):
    for term in available:
        if re.fullmatch(r"(?:请解释|解释|什么是)?" + re.escape(term) + r"(?:是什么意思|是什么|怎么理解)?", query):
            return [term]
    comparison = re.match(r"^(?:请比较|比较)?(.+?)(?:有什么区别|有何区别|的区别)$", query)
    if not comparison:
        return []
    body = comparison[1]
    for separator in re.finditer(r"[和与、]", body):
        left = body[:separator.start()].strip("「」“”")
        right = body[separator.end():].strip("「」“”")
        if left != right and left in available and right in available:
            return [left, right]
    return []


def answer(store, db, account, data):
    course_id = require_text(data, "course_id")
    question = require_text(data, "question")
    if len(question) > 200:
        raise invalid("question", "问题至多 200 字")
    row = published(store, db, account, course_id)
    by_term = {}
    seen = set()
    for item in json.loads(row["manifest"])["phrases"]:
        if not all(item.get(key) for key in ("term", "definition", "source")):
            continue
        term = normalize_term(item["term"])
        keys = ("term", "definition", "source")
        identity = tuple(item[key] for key in keys)
        if term and identity not in seen:
            citation = {key: item[key] for key in keys} | {"phrase_id":item["id"]}
            by_term.setdefault(term, []).append(citation)
            seen.add(identity)
    requested = requested_terms(normalize_term(question), by_term)
    matches = [citation for term in requested for citation in by_term[term]]
    return {"citations": matches, "comparison":len(requested) == 2,
            "reason": None if matches else "当前作业的已审知识卡没有可支持的释义。"}
