"""Conservative role proposals. No face/voice -> legal identity inference."""
import re

from .schema import Speaker

ROLES = ["身份待确认", "审判长", "审判员", "法官", "人民陪审员", "书记员", "原告", "被告", "被告人", "上诉人", "被上诉人", "申请人", "被申请人", "公诉人", "辩护人", "原告代理律师", "被告代理律师", "诉讼代理人", "证人", "鉴定人", "翻译人员", "公证人", "旁听人员", "法警", "陪审团成员", "其他"]

# Order matters: "我是原告的代理律师" must not become "原告".
PATTERNS = [
    ("原告代理律师", r"(?:我是|本人(?:是|系)|担任)(?:本案)?原告(?:方|的)?(?:委托)?(?:代理律师|诉讼代理人)"),
    ("被告代理律师", r"(?:我是|本人(?:是|系)|担任)(?:本案)?被告(?:方|的)?(?:委托)?(?:代理律师|诉讼代理人)"),
    ("辩护人", r"(?:我是|本人(?:是|系)|担任)(?:本案)?(?:被告人.{0,8}的)?辩护(?:人|律师)"),
] + [(role, rf"(?:我是|本人(?:是|系)|担任)(?:本案的?|本庭的?)?{re.escape(role)}(?!的|方|代理|辩护|律师|人)")
     for role in ["审判长", "审判员", "人民陪审员", "书记员", "被告人", "被上诉人", "上诉人", "被申请人", "申请人", "公诉人", "证人", "鉴定人", "翻译人员", "公证人", "法警", "原告", "被告"]]


def propose(segments):
    speakers = []
    for speaker_id in sorted({s.speaker_id for s in segments}):
        speaker = Speaker(id=speaker_id)
        candidates = {}
        for segment in segments:
            if segment.speaker_id != speaker_id or "overlap_unresolved" in segment.flags:
                continue
            for role, pattern in PATTERNS:
                if re.search(pattern, segment.original_text):
                    candidates.setdefault(role, []).append(segment.id)
                    break
        if len(candidates) == 1:
            speaker.role = next(iter(candidates))
            speaker.role_status = "suggested"
            speaker.evidence_segment_ids = candidates[speaker.role]
            speaker.evidence_note = "识别文本中有自述身份线索，尚需回听确认；ASR 自述不等于身份认证"
        elif len(candidates) > 1:
            speaker.evidence_note = "存在冲突的身份线索，可能是转述、声纹串人或识别错误"
            speaker.evidence_segment_ids = [sid for ids in candidates.values() for sid in ids]
        speakers.append(speaker)
    return speakers
