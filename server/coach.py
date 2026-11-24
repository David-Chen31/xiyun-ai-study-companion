"""Local LLM selection and interpretation of evidence-backed singing exercises."""

import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen


KEY_PATH = Path(__file__).resolve().parent.parent / ".xiyun" / "models" / "coach.key"
HAN = re.compile(r"[\u4e00-\u9fff]")
DELIVERY = {
    "restrained": "用接近说话的力度唱，句尾渐弱收束，留一点余韵。",
    "connected": "把前后几个字的元音连起来，保持旋律的线条，让重音自然出现。",
    "firm": "挑一个关键词稍加力度，辅音交代清楚，余下的字保持连贯。",
}
UNSUPPORTED = re.compile(r"唱得|实际|发音不准|跑调|抢拍|拖拍|漏唱|气息不足|声带|音分|半音|赫兹|\d")


def practice_choices(facts):
    location = "「" + facts["aligned_word"] + "」字" if facts.get("aligned_word") else facts.get("position", "这句")
    cents = facts.get("delta_cents")
    if cents is not None:
        actual = f"{location}的延音{('偏高' if cents > 0 else '偏低')}约 {abs(cents)} 音分。"
        correction = "往下收一点" if cents > 0 else "往上靠一点"
        note = "（参考音 " + facts["target_note"] + "）" if facts.get("target_note") else ""
        timed = max(.3, min(1.5, (facts.get("duration_ms", 500) / 1000)))
        options = {
            "hum": f"先慢听标记处{note}，用「嗯」轻哼同一音，{correction}，保持约 {timed:.1f} 秒。"
                   "音高稳住后换成原来的元音，最后把辅音和唱词接回去。",
            "vowel": f"单独练{location}{note}。先用元音对准目标音，{correction}，让延音保持在同一音高。"
                     "再带词连回前两个字，听句尾有没有滑回去。",
        }
    elif facts.get("register"):
        actual = "这句的可比旋律大致在另一个八度。"
        options = {
            "hum": "先用舒服的音区哼出同一句旋律，确认音程的方向，再带词，放轻力度。",
            "connect": "用现在的音区把相邻两个音连起来，保持同一元音，确认旋律走向后再唱整句。",
        }
    else:
        observed = facts["matched_percent"]
        actual = f"这句可比音高里约 {observed}% 贴近原唱。"
        options = {
            "connect": "按原速唱一遍，把相邻音之间的元音连起来。对听前后两个版本，保留贴近原唱的音头和延音。",
            "review": "先回听标记处，再把这一小段清唱一遍。注意音头一次到位、长音保持，最后恢复整句。",
        }
    return actual, options


def diction(lyric, facts):
    chars = HAN.findall(lyric)
    if not chars:
        return ""
    chosen = facts.get("aligned_word") or chars[-1]
    spelling = facts.get("pinyin", "")
    voice = "先按普通话慢读，再用字腹承载旋律的延音。"
    try:
        from pypinyin import Style, lazy_pinyin
    except ImportError:
        pass
    else:
        text = "".join(chars)
        start = text.rfind(chosen)
        if start >= 0 and not spelling:
            pronunciations = lazy_pinyin(text, style=Style.TONE, neutral_tone_with_five=True)
            spelling = " ".join(pronunciations[start:start + len(chosen)])
        if start >= 0 and len(chosen) == 1:
            initial = lazy_pinyin(text, style=Style.INITIALS)[start]
            final = lazy_pinyin(text, style=Style.FINALS)[start]
            beginning = f"字头 {initial} 短而清楚，" if initial else "元音干净起声，"
            ending = "最后归到后鼻音 ng。" if final.endswith("ng") else (
                "最后归到前鼻音 n。" if final.endswith("n") else "收音跟着元音自然结束。"
            )
            voice = beginning + f"让 {final} 的元音部分延长，" + ending
    spell = f"（{spelling}）" if spelling else ""
    return f"用「{chosen}」{spell}做咬字示范。" + voice


def coach(lyric, facts):
    base = facts["pitch_score"]
    radius = 1 if facts["coverage"] >= 70 else 3
    low, high = max(0, base - radius), min(100, base + radius)
    assessment, exercises = practice_choices(facts)
    schema = {
        "type": "object",
        "properties": {
            "score": {"type": "integer", "minimum": low, "maximum": high},
            "exercise": {"type": "string", "enum": list(exercises)},
            "delivery": {"type": "string", "enum": list(DELIVERY)},
            "reading": {"type": "string"},
        },
        "required": ["score", "exercise", "delivery", "reading"],
        "additionalProperties": False,
    }
    ask = (
        "你是中文歌唱教练。为一句歌词选择最适合的练法和情感表达。"
        "score 是练习分，证据充分就沿用音高分；音高分不能推断情感、咬字或气息。"
        "exercise 从给定练法选一个代号，优先更贴合音高偏差位置的。"
        "delivery 从给定表演方法选一个代号。reading 用十五到三十个汉字解释歌词里的处境或情绪，"
        "例如‘像是在平静地承认关系已经改变’。用自己的话说明语气，避免复述原句。"
        "只解读歌词，不评论录音，不提技术，不编造情节。"
    )
    inputs = {"唱词": lyric, "测量": facts, "练法": exercises, "表演方法": DELIVERY}
    evidence = json.dumps(inputs, ensure_ascii=False).replace("<|", "< |")
    payload = {
        "prompt": f"<|im_start|>system\n{ask}<|im_end|>\n<|im_start|>user\n{evidence}<|im_end|>\n"
                  "<|im_start|>assistant\n<think>\n\n</think>\n\n",
        "json_schema": schema,
        "n_predict": 170,
        "stop": ["<|im_end|>"],
        "temperature": 0.2,
    }
    port = int(os.environ.get("XIYUN_COACH_PORT", "5188"))
    headers = {"Content-Type": "application/json"}
    if KEY_PATH.is_file():
        headers["Authorization"] = "Bearer " + KEY_PATH.read_text(encoding="ascii").strip()
    request = Request(f"http://127.0.0.1:{port}/completion",
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers=headers)
    with urlopen(request, timeout=25) as response:
        generated = json.loads(response.read(65_536))
    result = json.loads(generated["content"])
    if (not isinstance(result, dict) or type(result.get("score")) is not int
            or not low <= result["score"] <= high or result.get("exercise") not in exercises
            or result.get("delivery") not in DELIVERY):
        raise ValueError("模型点评超出测量范围")
    reading = result.get("reading", "")
    if not isinstance(reading, str) or len(reading) > 85 or not HAN.search(reading) or UNSUPPORTED.search(reading):
        reading = ""
    elif "".join(HAN.findall(reading)) in "".join(HAN.findall(lyric)):
        reading = ""
    reading = reading.strip(" 。。，")
    return {
        "score": result["score"], "assessment": assessment, "exercise": exercises[result["exercise"]],
        "expression": (reading + "。 " if reading else "") + DELIVERY[result["delivery"]],
        "diction": diction(lyric, facts),
    }
