from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class Options(Model):
    engine: Literal["local", "tencent"] = "local"
    language: str = Field(default="zh", pattern=r"^(auto|[a-z]{2,3})$")
    diarize: bool = True
    separate_overlap: bool = False
    num_speakers: int | None = Field(default=None, ge=1, le=50)
    audio_stream: int = Field(default=0, ge=0, le=31)
    cloud_consent: bool = False
    role_agent: bool = False
    visual_evidence: bool = False

    @model_validator(mode="after")
    def consistency(self):
        if self.engine == "tencent" and not self.cloud_consent:
            raise ValueError("使用云端识别须明确同意发送本任务音频")
        if self.separate_overlap and (not self.diarize or self.engine != "local"):
            raise ValueError("实验性重叠分离需要本地识别及说话人分离")
        if self.visual_evidence and not self.role_agent:
            raise ValueError("视觉证据需要启用角色分析 Agent")
        return self


class Word(Model):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    probability: float | None = Field(default=None, ge=0, le=1)


class Segment(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    speaker_id: str
    text: str = Field(max_length=50000)
    original_text: str
    words: list[Word] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    overlap_group: str | None = None
    reviewed: bool = False
    source: str = "asr"

    @model_validator(mode="after")
    def ordered(self):
        if self.end < self.start:
            raise ValueError("结束时间不能早于开始时间")
        return self


class Speaker(Model):
    id: str
    name: str = Field(default="", max_length=100)
    role: str = Field(default="身份待确认", max_length=100)
    role_status: Literal["unknown", "suggested", "confirmed"] = "unknown"
    evidence_segment_ids: list[str] = Field(default_factory=list)
    evidence_note: str = Field(default="", max_length=2000)
    evidence_frame_seconds: list[float] = Field(default_factory=list)


class Task(Model):
    id: str = Field(default_factory=lambda: uuid4().hex)
    title: str = Field(max_length=250)
    filename: str
    state: Literal["queued", "running", "completed", "failed", "cancelled", "interrupted"] = "queued"
    stage: str = "等待处理"
    progress: int = Field(default=0, ge=0, le=100)
    created_at: str = Field(default_factory=now)
    updated_at: str = Field(default_factory=now)
    revision: int = 0
    is_demo: bool = False
    options: Options = Field(default_factory=Options)
    media: dict = Field(default_factory=dict)
    manifest: dict = Field(default_factory=dict)
    speakers: list[Speaker] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None


class SegmentEdit(Model):
    revision: int = Field(ge=0)
    text: str = Field(max_length=50000)
    speaker_id: str = Field(min_length=1, max_length=100)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    reviewed: bool = False


class SpeakerEdit(Model):
    revision: int = Field(ge=0)
    name: str = Field(default="", max_length=100)
    role: str = Field(min_length=1, max_length=100)
    role_status: Literal["unknown", "suggested", "confirmed"]
    evidence_segment_ids: list[str] = Field(default_factory=list, max_length=50)
    evidence_note: str = Field(default="", max_length=2000)
