import html
import json

from .schema import Task

FLAG_NAMES = {
    "speaker_unassigned": "说话人待分配", "speaker_uncertain": "说话人存疑", "low_confidence": "低置信片段",
    "overlap_unresolved": "重叠发言未完整还原", "overlap_review": "重叠发言待核听",
    "inaudible": "内容待核听", "coarse_timestamps": "时间仅到句段",
    "separated_experimental": "实验性声部分离", "cloud_asr_review": "云端识别待复核",
}


def timestamp(seconds: float):
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3600000)
    minutes, milliseconds = divmod(milliseconds, 60000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02}.{milliseconds:03}"


def safe(text: str):
    # Markdown is a transport, not an opportunity to reinterpret transcript text as links/HTML.
    text = html.escape(text, quote=False)
    for char in ["\\", "`", "*", "_", "[", "]", "|", "#", ">", "~"]:
        text = text.replace(char, "\\" + char)
    return text


def markdown(task: Task):
    speakers = {s.id: s for s in task.speakers}
    lines = [f"# {safe(task.title)}", "", "> 庭审逐字转写辅助稿。正文保留识别文本或人工校对文本，未做摘要、润色或法律判断。", ""]
    if task.is_demo:
        lines += ["> 演示数据：人为编写的合成片段，不是视频识别结果。", ""]
    lines += [f"- 任务 ID：`{task.id}`", f"- 原始文件：{safe(task.filename)}", f"- 输入 SHA256：`{task.manifest.get('input_sha256', '演示无输入文件')}`",
              f"- 转写版本：{task.revision}", f"- 引擎：{task.options.engine}", f"- 管线版本：{task.manifest.get('pipeline_version', '0.1.0')}",
              f"- 已核听：{sum(s.reviewed for s in task.segments)} / {len(task.segments)} 段", "", "## 说话人与角色", "",
              "同一声纹编号不保证身份正确；角色“候选”须核对。没有发声的人无法仅凭声轨列出发言。", ""]
    for speaker in task.speakers:
        status = {"unknown": "待确认", "suggested": "候选，待确认", "confirmed": "人工确认"}[speaker.role_status]
        lines.append(f"- **{safe(speaker.id)}**：{safe(speaker.name or '未命名')} / {safe(speaker.role)}（{status}）")
        if speaker.evidence_note:
            lines.append(f"  - 依据：{safe(speaker.evidence_note)}；片段 ID：{', '.join(speaker.evidence_segment_ids) or '无'}")
        if speaker.evidence_frame_seconds:
            lines.append("  - 候选画面时刻：" + "、".join(timestamp(t) for t in speaker.evidence_frame_seconds))
    if task.warnings:
        lines += ["", "## 待核实事项", ""] + [f"- {safe(w)}" for w in task.warnings]
    lines += ["", "## 按庭审时间顺序的发言", "", "重叠发言按开始时间排序，保留各自起止时间；同时发言不代表先后应答关系。", ""]
    for segment in sorted(task.segments, key=lambda s: (s.start, s.end, s.id)):
        speaker = speakers.get(segment.speaker_id)
        label = speaker.name or segment.speaker_id if speaker else segment.speaker_id
        role = speaker.role if speaker else "身份待确认"
        if speaker and speaker.role_status == "suggested":
            role += "（候选）"
        elif speaker and speaker.role_status == "unknown" and role != "身份待确认":
            role += "（待确认）"
        notes = ["已人工核听" if segment.reviewed else "未核听"]
        if segment.overlap_group:
            notes.append(f"同时发言 {segment.overlap_group}")
        notes += [FLAG_NAMES.get(f, f) for f in segment.flags]
        lines += [f"### {timestamp(segment.start)} → {timestamp(segment.end)} · {safe(label)} · {safe(role)}", "",
                  f"<!-- segment_id: {segment.id}; source: {segment.source} -->", "",
                  "\n".join("> " + safe(line) for line in segment.text.split("\n")), "", f"*状态：{'；'.join(notes)}*", ""]
    return "\n".join(lines)


def json_export(task):
    return json.dumps(task.model_dump(mode="json"), ensure_ascii=False, indent=2)
