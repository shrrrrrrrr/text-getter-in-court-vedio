"""Explicit synthetic UI fixture; never used as an ASR fallback."""
from .schema import Segment, Speaker, Task


def demo():
    segments = [
        Segment(id="demo-1", start=0.8, end=5.9, speaker_id="SPEAKER_00", text="现在开庭。首先核对双方当事人的身份。", original_text="现在开庭。首先核对双方当事人的身份。", source="synthetic"),
        Segment(id="demo-2", start=6.8, end=10.6, speaker_id="SPEAKER_01", text="我是原告，嗯，我的姓名是陈某。", original_text="我是原告，嗯，我的姓名是陈某。", source="synthetic"),
        Segment(id="demo-3", start=11.6, end=15.9, speaker_id="SPEAKER_02", text="我是被告的代理律师，相关材料已经提交。", original_text="我是被告的代理律师，相关材料已经提交。", source="synthetic"),
        Segment(id="demo-4", start=17.2, end=21.5, speaker_id="SPEAKER_01", text="但是那个材料，我，我没有收到过。", original_text="但是那个材料，我，我没有收到过。", flags=["overlap_review"], overlap_group="overlap-1", source="synthetic"),
        Segment(id="demo-5", start=18.6, end=22.4, speaker_id="SPEAKER_02", text="我们在上周就已经邮寄了。", original_text="我们在上周就已经邮寄了。", flags=["overlap_review"], overlap_group="overlap-1", source="synthetic"),
        Segment(id="demo-6", start=23.5, end=27.3, speaker_id="SPEAKER_00", text="请双方依次发言。原告先说。", original_text="请双方依次发言。原告先说。", source="synthetic"),
        Segment(id="demo-7", start=28.2, end=32.5, speaker_id="UNKNOWN", text="[听不清，待核听]", original_text="[听不清，待核听]", flags=["inaudible", "speaker_unassigned"], source="synthetic"),
    ]
    speakers = [
        Speaker(id="SPEAKER_00", role="法官", role_status="suggested", evidence_segment_ids=["demo-1"], evidence_note="仅为演示角色候选，不代表已确认真实身份"),
        Speaker(id="SPEAKER_01", role="原告", role_status="suggested", evidence_segment_ids=["demo-2"], evidence_note="合成文本中的自我介绍"),
        Speaker(id="SPEAKER_02", role="被告代理律师", role_status="suggested", evidence_segment_ids=["demo-3"], evidence_note="合成文本中的自我介绍"),
        Speaker(id="UNKNOWN"),
    ]
    return Task(title="庭审逐字稿 · 交互演示", filename="合成示例 · 无真实视频", is_demo=True,
                state="completed", stage="演示数据", progress=100, media={"duration": 34, "has_video": False},
                speakers=speakers, segments=segments, manifest={"pipeline_version": "0.1.0", "input_sha256": "synthetic-no-source"},
                warnings=["这些发言是人工编写的合成演示，不是上传视频的识别结果；没有真实声轨可供回放"])
