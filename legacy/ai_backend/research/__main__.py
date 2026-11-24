"""在经授权的独立样本上复跑声学研究基线。"""

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .acoustics import entry_candidate, pitch_track, voiced_entry_candidate
from .acoustic_alignment import acoustic_alignment
from .alignment import anchor_map
from .rhythm import rhythm_candidates


def read_approved(path, manifest):
    permission = manifest.get("permission")
    if not isinstance(permission, dict) or permission.get("purpose") != "offline_acoustic_evaluation":
        raise ValueError("请提供专项离线评测的许可记录")
    reference = permission.get("reference")
    if not isinstance(reference, str) or not reference.strip():
        raise ValueError("请填写可复核的许可引用")
    expiry = datetime.fromisoformat(permission.get("expires_at", ""))
    if expiry.utcoffset() != timezone.utc.utcoffset(expiry) or expiry <= datetime.now(timezone.utc):
        raise ValueError("许可须有有效 UTC 到期时间")
    source = path.parent / manifest["audio"]
    with source.open("rb") as file:
        wav = file.read(2 * 1024 * 1024 + 1)
    if len(wav) > 2 * 1024 * 1024 or hashlib.sha256(wav).hexdigest() != permission.get("sha256"):
        raise ValueError("样本超限或摘要与许可记录不一致")
    return wav


def main(argv=None):
    parser = argparse.ArgumentParser(description="戏韵声学离线研究基线")
    parser.add_argument("--manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = json.loads(args.manifest.read_text(encoding="utf-8-sig"))
        method = manifest["method"]
        if method == "anchors":
            result = anchor_map(manifest)
        elif method == "acoustic-align":
            left = read_approved(args.manifest, manifest["left"])
            right = read_approved(args.manifest, manifest["right"])
            result = acoustic_alignment(manifest, left, right)
        elif method in ("entry", "entry-voiced", "pitch", "rhythm"):
            wav = read_approved(args.manifest, manifest)
            if method == "entry":
                result = entry_candidate(wav, manifest)
            elif method == "entry-voiced":
                result = voiced_entry_candidate(wav, manifest)
            elif method == "rhythm":
                result = rhythm_candidates(wav, manifest)
            else:
                result = {"frames": pitch_track(wav)}
        else:
            raise ValueError("未知方法，请选择 entry、entry-voiced、pitch、rhythm、anchors 或 acoustic-align")
        baseline = "offline-voiced-2" if method == "entry-voiced" else "offline-1"
        print(json.dumps({"method": method, "baseline": baseline, "result": result}, ensure_ascii=False))
    except (OSError, KeyError, ValueError, TypeError) as error:
        print(f"研究样本未处理：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
