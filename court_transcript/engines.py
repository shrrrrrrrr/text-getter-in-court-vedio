from __future__ import annotations

import base64
import importlib.metadata
import importlib.util
import json
import os
import time
import wave
from pathlib import Path
from contextlib import contextmanager

from .alignment import Turn, align, annotate_overlaps
from .media import check_cancel, cloud_audio
from .schema import Segment, Word


@contextmanager
def offline_hub(offline):
    """Hub reads its offline flag at import time, so setting os.environ alone is insufficient."""
    if not offline:
        yield
        return
    import huggingface_hub.constants as constants
    old = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = True
    try:
        yield
    finally:
        constants.HF_HUB_OFFLINE = old


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ModuleNotFoundError, ValueError):
        return False


def package_versions():
    versions = {}
    for name in ["faster-whisper", "ctranslate2", "av", "onnxruntime", "numpy", "soundfile", "fastapi", "uvicorn", "pydantic", "pyannote.audio", "speechbrain", "torch", "tencentcloud-sdk-python-asr"]:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            pass
    return versions


def model_identity(model_name):
    """Record resolved snapshot revision and weight digests when a local directory is used."""
    from .media import sha256
    path = Path(model_name)
    if not path.is_dir():
        return {"requested": model_name, "resolved_revision": None, "reproducibility": "版本未固定；正式测评请使用固定 revision 的本地快照目录"}
    paths = [p for p in path.rglob("*") if p.is_file() and p.suffix in {".bin", ".json", ".yaml", ".safetensors", ".pt", ".onnx"}]
    return {"requested": path.name, "resolved_revision": path.name if len(path.name) == 40 else None,
            "files_sha256": {str(p.relative_to(path)): sha256(p) for p in sorted(paths)},
            "reproducibility": "本地文件指纹已记录"}


def capabilities(settings):
    return {
        "local_asr": installed("faster_whisper"),
        "diarization": installed("pyannote.audio"),
        "overlap_separation": installed("speechbrain"),
        "tencent": installed("tencentcloud.asr") and bool(os.getenv("TENCENTCLOUD_SECRET_ID") and os.getenv("TENCENTCLOUD_SECRET_KEY")),
        "model": Path(settings.model).name,
        "device": settings.device,
        "allow_download": settings.allow_download,
        "model_ready": "unknown_until_load",
    }


class LocalEngine:
    def __init__(self, settings):
        self.settings = settings
        self.asr = None
        self.diarizer = None
        self.separator = None
        self.identity = None

    def load_asr(self):
        if not installed("faster_whisper"):
            raise RuntimeError('尚未安装本地 ASR；运行 python -m pip install -e ".[local]"')
        if self.asr is None:
            from faster_whisper import WhisperModel
            try:
                self.asr = WhisperModel(self.settings.model, device=self.settings.device,
                                        compute_type=self.settings.compute_type,
                                        local_files_only=not self.settings.allow_download)
            except Exception as exc:
                raise RuntimeError("本地识别模型无法加载；请检查模型路径/缓存、计算类型，或显式允许下载模型") from exc
        return self.asr

    def transcribe(self, path, language, event, offset=0.0, progress=None):
        model = self.load_asr()
        if self.identity is None:
            self.identity = model_identity(self.settings.model)
        chunks, info = model.transcribe(str(path), language=None if language == "auto" else language,
                                       beam_size=5, temperature=0, word_timestamps=True,
                                       vad_filter=True, condition_on_previous_text=False,
                                       initial_prompt=None)
        result = []
        for chunk in chunks:
            check_cancel(event)
            words = [Word(start=w.start + offset, end=w.end + offset, text=w.word, probability=w.probability)
                     for w in (chunk.words or [])]
            flags = ["low_confidence"] if chunk.avg_logprob < -0.8 or chunk.no_speech_prob > 0.5 else []
            result.append(Segment(start=chunk.start + offset, end=chunk.end + offset, speaker_id="UNKNOWN",
                                  text=chunk.text, original_text=chunk.text, words=words, flags=flags))
            if progress:
                progress(chunk.end, float(info.duration))
        return result, info.language

    def diarize(self, path, options, event):
        if not installed("pyannote.audio"):
            raise RuntimeError('说话人分离未安装；运行 python -m pip install -e ".[diarization]"，或关闭说话人分离仅转写')
        import numpy as np
        import torch
        from pyannote.audio import Pipeline
        model = os.getenv("COURT_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1")
        if self.diarizer is None:
            try:
                with offline_hub(not self.settings.allow_download):
                    self.diarizer = Pipeline.from_pretrained(model, token=os.getenv("HF_TOKEN") or None)
                self.diarizer.to(torch.device(self.settings.device))
            except Exception as exc:
                raise RuntimeError("说话人模型无法加载：请检查本地权重、HF_TOKEN 及模型访问条款是否已接受") from exc
        check_cancel(event)
        with wave.open(str(path), "rb") as f:
            waveform = np.frombuffer(f.readframes(f.getnframes()), dtype="<i2").astype("float32") / 32768
        params = {"num_speakers": options.num_speakers} if options.num_speakers else {}
        output = self.diarizer({"waveform": torch.from_numpy(waveform.copy()).unsqueeze(0), "sample_rate": 16000}, **params)
        check_cancel(event)
        # Use regular diarization, never exclusive_speaker_diarization (which discards overlaps).
        annotation = getattr(output, "speaker_diarization", output)
        return [Turn(float(turn.start), float(turn.end), str(speaker)) for turn, _, speaker in annotation.itertracks(yield_label=True)]

    def separate(self, audio_path, directory, segments, windows, options, event, update):
        if not installed("speechbrain"):
            raise RuntimeError('实验性语音分离未安装；运行 python -m pip install -e ".[overlap]"')
        import numpy as np
        import soundfile as sf
        import torch
        from speechbrain.inference.separation import SepformerSeparation
        model = os.getenv("COURT_SEPARATION_MODEL", "speechbrain/sepformer-whamr16k")
        if self.separator is None:
            with offline_hub(not self.settings.allow_download):
                self.separator = SepformerSeparation.from_hparams(source=model,
                    savedir=str(directory.parent / "separation-model"), run_opts={"device": self.settings.device})
        waveform, sr = sf.read(audio_path, dtype="float32")
        alternatives = []
        for index, (a, b, speakers) in enumerate(windows):
            check_cancel(event)
            group = f"overlap-{index + 1}"
            # This model only supports two sources; don't pretend to separate 3+ voices.
            if len(speakers) != 2 or b - a > 15:
                continue
            update(f"实验性重叠分离 {index + 1}/{len(windows)}", None)
            mixture = torch.from_numpy(waveform[int(a * sr):int(b * sr)].copy()).unsqueeze(0)
            with torch.inference_mode():
                sources = self.separator.separate_batch(mixture.to(self.settings.device)).detach().cpu().numpy()[0]
            separated = []
            for track in range(sources.shape[-1]):
                track_id = f"OVL_{index + 1}_{track + 1}"
                path = directory / f"{track_id}.wav"
                sf.write(path, np.clip(sources[:, track], -1, 1), sr, subtype="PCM_16")
                raw, _ = self.transcribe(path, options.language, event, offset=a)
                for segment in raw:
                    segment.speaker_id = track_id
                    segment.source = "separated"
                    segment.overlap_group = group
                    segment.flags = sorted(set(segment.flags + ["separated_experimental", "speaker_unassigned", "overlap_review"]))
                    separated.append(segment)
            if len({s.speaker_id for s in separated}) == 2:
                replaced = [s for s in segments if s.overlap_group == group]
                alternatives.extend(s.model_dump(mode="json") for s in replaced)
                segments = [s for s in segments if s.overlap_group != group] + separated
        (directory / "mixture-alternatives.json").write_text(json.dumps(alternatives, ensure_ascii=False, indent=2), "utf-8")
        return sorted(segments, key=lambda s: (s.start, s.end, s.speaker_id))

    def run(self, path, directory, options, event, update):
        update("加载本地语音识别模型", 18)
        raw, language = self.transcribe(path, options.language, event,
            progress=lambda done, total: update("逐字转写", min(58, 20 + int(38 * done / max(total, 1)))))
        (directory / "asr-raw.json").write_text(json.dumps([s.model_dump(mode="json") for s in raw], ensure_ascii=False, indent=2), "utf-8")
        warnings = []
        if options.diarize:
            update("说话人聚类与重叠检测", 62)
            turns = self.diarize(path, options, event)
            segments, windows = align(raw, turns)
            (directory / "diarization.json").write_text(json.dumps([vars(t) for t in turns], ensure_ascii=False, indent=2), "utf-8")
            if options.separate_overlap and windows:
                segments = self.separate(path, directory, segments, windows, options, event, update)
                warnings.append("重叠分离为实验功能；每段独立分离声轨的跨段身份未自动确认，须核听后合并到人物")
            if windows:
                warnings.append("已发现重叠发言；未还原的声部保留占位，分离结果及说话人归属须逐段核听")
        else:
            segments = raw
            windows = []
            for segment in segments:
                segment.flags.append("speaker_unassigned")
            warnings.append("本任务关闭了说话人分离：无法判断人数及重叠发言，不代表只有一个人")
        return segments, warnings, {"detected_language": language, "overlap_windows": len(windows),
            "asr_model": self.settings.model, "diarization_model": os.getenv("COURT_DIARIZATION_MODEL", "pyannote/speaker-diarization-community-1") if options.diarize else None,
            "asr_identity": self.identity,
            "decode": {"beam_size": 5, "temperature": 0, "word_timestamps": True, "vad_filter": True, "condition_on_previous_text": False, "initial_prompt": None}}


def parse_tencent(data):
    result = []
    for item in data.get("ResultDetail") or []:
        text = item.get("FinalSentence", "")
        start, end = item["StartMs"] / 1000, item["EndMs"] / 1000
        speaker = f"SPEAKER_{item['SpeakerId']}" if item.get("SpeakerId") is not None and item["SpeakerId"] >= 0 else "UNKNOWN"
        result.append(Segment(start=start, end=end, text=text, original_text=text, speaker_id=speaker,
                              flags=["cloud_asr_review"], source="tencent"))
    if not result and data.get("Result"):
        raise RuntimeError("云端未返回带说话人及时间戳的详细结果，原始返回已本地保留")
    annotate_overlaps(result)
    return result


def tencent_run(path, directory, options, event, update):
    if not capabilities(type("S", (), {"model": "", "device": "", "allow_download": False})())["tencent"]:
        raise RuntimeError("腾讯云适配器尚未配置 SDK 和服务器环境变量凭证")
    from tencentcloud.common import credential
    from tencentcloud.common.profile.client_profile import ClientProfile
    from tencentcloud.common.profile.http_profile import HttpProfile
    from tencentcloud.asr.v20190614 import asr_client, models
    profile = ClientProfile(httpProfile=HttpProfile(reqTimeout=60))
    client = asr_client.AsrClient(credential.Credential(os.environ["TENCENTCLOUD_SECRET_ID"], os.environ["TENCENTCLOUD_SECRET_KEY"]),
                                 os.getenv("TENCENTCLOUD_REGION", "ap-shanghai"), profile)
    checkpoint = directory / "tencent-job.json"
    if checkpoint.exists():
        job = json.loads(checkpoint.read_text("utf-8"))
        if not job.get("task_id"):
            raise RuntimeError("云端提交结果未知，已阻止自动重复提交；请先在腾讯云控制台核对任务及计费")
    else:
        update("准备云端音频（将发送至腾讯云）", 20)
        target = directory / "cloud.mp3"
        cloud_audio(path, target, event)
        if target.stat().st_size > 5_000_000:
            raise RuntimeError("腾讯云本地数据提交限制为 5 MB；本适配器不自动切片以避免跨片说话人错配。长视频请使用本地管线")
        params = {"EngineModelType": os.getenv("COURT_TENCENT_ENGINE", "16k_zh_en_meeting"), "ChannelNum": 1,
                  "ResTextFormat": 2, "SourceType": 1, "Data": base64.b64encode(target.read_bytes()).decode(),
                  "SpeakerDiarization": 1, "SpeakerNumber": min(options.num_speakers or 0, 10),
                  "FilterDirty": 0, "FilterModal": 0, "FilterPunc": 0, "ConvertNumMode": 0,
                  "SentenceMaxLength": 0}
        request = models.CreateRecTaskRequest()
        request.from_json_string(json.dumps(params))
        check_cancel(event)
        # Write before network I/O: unknown submission must not be automatically replayed.
        checkpoint.write_text(json.dumps({"submission": "started", "task_id": None}), "utf-8")
        response = client.CreateRecTask(request)
        job = {"task_id": response.Data.TaskId, "engine": params["EngineModelType"]}
        checkpoint.write_text(json.dumps(job), "utf-8")
    deadline = time.monotonic() + 3 * 3600
    while time.monotonic() < deadline:
        check_cancel(event)
        update("等待腾讯云转写结果（本地取消不会撤销云端任务）", 50)
        request = models.DescribeTaskStatusRequest()
        request.TaskId = job["task_id"]
        payload = json.loads(client.DescribeTaskStatus(request).to_json_string())["Data"]
        (directory / "tencent-raw.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        if payload["Status"] == 2:
            return parse_tencent(payload), ["腾讯云不提供本适配器可验证的重叠声部完整性，未返回重叠不代表不存在；请核听"], {
                "asr_model": job.get("engine"), "remote_task_id": job["task_id"],
                "decode": {"FilterDirty": 0, "FilterModal": 0, "FilterPunc": 0, "ConvertNumMode": 0, "ResTextFormat": 2}}
        if payload["Status"] == 3:
            raise RuntimeError("腾讯云识别任务失败，原始结果已保存；请核对云端控制台")
        if event.wait(3):
            check_cancel(event)
    raise RuntimeError("等待云端结果超时；可恢复查询原任务，避免重复计费")
