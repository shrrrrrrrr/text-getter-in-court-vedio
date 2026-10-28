import hashlib
import json

import pytest
from pydantic import ValidationError

from court_transcript.alignment import Turn, align, overlap_windows
from court_transcript.demo import demo
from court_transcript.engines import parse_tencent
from court_transcript.export import markdown, timestamp
from court_transcript.roles import propose
from court_transcript.schema import Options, Segment, Word
from court_transcript.store import Conflict, Store


def test_overlap_does_not_copy_recognized_words():
    raw = [Segment(start=0, end=3, speaker_id="UNKNOWN", text="我，我没收到。", original_text="我，我没收到。",
                   words=[Word(start=0, end=.8, text="我，"), Word(start=1, end=1.8, text="我"), Word(start=2, end=3, text="没收到。")])]
    result, windows = align(raw, [Turn(0, 3, "A"), Turn(1, 2.5, "B")])
    observed = [s for s in result if s.source != "unresolved"]
    assert "".join(s.text for s in sorted(observed, key=lambda s: s.start)) == "我，我没收到。"
    assert sum(len(s.words) for s in observed) == 3
    assert windows == [(1, 2.5, ("A", "B"))]
    assert any(s.speaker_id == "B" and "inaudible" in s.flags for s in result)


def test_same_speaker_overlapping_turns_not_two_people():
    assert overlap_windows([Turn(0, 2, "A"), Turn(1, 3, "A")]) == []
    assert overlap_windows([Turn(0, 1, "A"), Turn(1, 2, "B")]) == []


def test_unassigned_and_low_confidence_preserved():
    raw = [Segment(start=1, end=2, speaker_id="UNKNOWN", text="嗯", original_text="嗯", flags=["low_confidence"])]
    result, _ = align(raw, [])
    assert set(result[0].flags) >= {"low_confidence", "speaker_unassigned", "coarse_timestamps"}


@pytest.mark.parametrize("text,role", [("我是原告，我叫张某。", "原告"), ("我是原告的代理律师。", "原告代理律师"), ("我是被告的诉讼代理人。", "被告代理律师"), ("本人系本案的书记员。", "书记员")])
def test_role_suggestions_need_review(text, role):
    s = Segment(start=0, end=3, speaker_id="A", text=text, original_text=text)
    p = propose([s])[0]
    assert p.role == role and p.role_status == "suggested"
    assert p.evidence_segment_ids == [s.id]


def test_addressing_someone_does_not_identify_speaker():
    for text in ["法官您好，我有异议。", "原告请起立。", "现在开庭。", "我是原告的朋友。", "我是被告人的母亲。"]:
        p = propose([Segment(start=0, end=2, speaker_id="A", text=text, original_text=text)])[0]
        assert p.role_status == "unknown"


def test_role_conflict_stays_unknown():
    segments = [Segment(start=i, end=i+1, speaker_id="A", text=text, original_text=text) for i, text in enumerate(["我是原告。", "我是被告。"])]
    assert propose(segments)[0].role_status == "unknown"


def test_markdown_stable_overlap_and_verbatim():
    task = demo()
    original = task.segments[3].text
    task.segments.reverse()
    text = markdown(task)
    assert original in text
    assert text.index("00:17.200") < text.index("00:18.600")
    assert "候选" in text and "合成" in text and "未核听" in text
    task.segments[0].text = "<script>alert(1)</script> [链接](https://invalid.test)"
    result = markdown(task)
    assert "<script>" not in result and "\\[链接\\]" in result
    assert timestamp(3599.9996) == "01:00:00.000"


def test_store_conflict_original_text_and_audit_chain(tmp_path):
    store = Store(tmp_path)
    task = demo()
    store.save(task)
    original = task.segments[0].original_text
    edited = store.edit(task.id, 0, "text", lambda t: setattr(t.segments[0], "text", "人工校对"))
    assert edited.segments[0].original_text == original
    with pytest.raises(Conflict):
        store.edit(task.id, 0, "stale", lambda t: None)
    store.edit(task.id, 1, "review", lambda t: setattr(t.segments[0], "reviewed", True))
    entries = [json.loads(line) for line in (store.directory(task.id)/"audit.jsonl").read_text("utf-8").splitlines()]
    assert entries[1]["prev"] == entries[0]["hash"]
    for entry in entries:
        digest = entry.pop("hash")
        assert digest == hashlib.sha256(json.dumps(entry, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    with pytest.raises(FileNotFoundError):
        store.directory("../other")


def test_options_require_explicit_cloud_consent():
    with pytest.raises(ValidationError):
        Options(engine="tencent")
    with pytest.raises(ValidationError):
        Options(diarize=False, separate_overlap=True)
    with pytest.raises(ValidationError):
        Options(audio_stream=-1)


def test_cloud_overlap_independent_texts():
    parsed = parse_tencent({"ResultDetail": [{"StartMs": 0, "EndMs": 2000, "SpeakerId": 0, "FinalSentence": "我不同意。"}, {"StartMs": 1000, "EndMs": 3000, "SpeakerId": 1, "FinalSentence": "请听我说。"}]})
    assert len(parsed) == 2 and parsed[0].text != parsed[1].text
    assert parsed[0].overlap_group == parsed[1].overlap_group


def test_nonfinite_timestamps_rejected():
    with pytest.raises(ValidationError):
        Segment(start=float('nan'), end=4, speaker_id="A", text="", original_text="")
