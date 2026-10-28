"""Assign each observed word once. Overlap never means copying one text to two people."""
from dataclasses import dataclass

from .schema import Segment, Word


@dataclass
class Turn:
    start: float
    end: float
    speaker: str


def intersection(a, b, c, d):
    return max(0.0, min(b, d) - max(a, c))


def overlap_windows(turns: list[Turn], minimum=0.15):
    events = {}
    for t in turns:
        if t.end <= t.start:
            continue
        events.setdefault(t.start, []).append((t.speaker, 1))
        events.setdefault(t.end, []).append((t.speaker, -1))
    active, previous, windows = {}, None, []
    for at in sorted(events):
        speakers = tuple(sorted(s for s, count in active.items() if count > 0))
        if previous is not None and len(speakers) >= 2:
            if windows and windows[-1][1] == previous and windows[-1][2] == speakers:
                windows[-1] = (windows[-1][0], at, speakers)
            else:
                windows.append((previous, at, speakers))
        for speaker, delta in events[at]:
            active[speaker] = active.get(speaker, 0) + delta
        previous = at
    return [w for w in windows if w[1] - w[0] >= minimum]


def align(raw: list[Segment], turns: list[Turn]):
    windows = overlap_windows(turns)
    result: list[Segment] = []
    for original in raw:
        # A provider without word timestamps can only be assigned at utterance granularity.
        words = original.words or [Word(start=original.start, end=original.end, text=original.text)]
        for word in words:
            scores = {}
            for turn in turns:
                score = intersection(word.start, word.end, turn.start, turn.end)
                scores[turn.speaker] = scores.get(turn.speaker, 0) + score
            ranked = sorted(scores.items(), key=lambda p: (-p[1], p[0]))
            speaker = ranked[0][0] if ranked and ranked[0][1] > 0 else "UNKNOWN"
            flags = original.flags.copy()
            if speaker == "UNKNOWN":
                flags.append("speaker_unassigned")
            if len(ranked) > 1 and ranked[1][1] > 0.35 * max(0.01, word.end - word.start):
                flags.append("speaker_uncertain")
            if word.probability is not None and word.probability < 0.65:
                flags.append("low_confidence")
            midpoint = (word.start + word.end) / 2
            group = next((f"overlap-{i + 1}" for i, (a, b, _) in enumerate(windows) if a <= midpoint < b), None)
            if group:
                flags.append("overlap_unresolved")
            if not original.words:
                flags.append("coarse_timestamps")
            can_join = bool(result and result[-1].speaker_id == speaker and result[-1].overlap_group == group
                            and word.start - result[-1].end < 0.8 and word.end - result[-1].start < 16
                            and not result[-1].text.endswith(("。", "！", "？", "!", "?", "\n")))
            if can_join:
                segment = result[-1]
                segment.text += word.text
                segment.original_text = segment.text
                segment.end = max(segment.end, word.end)
                segment.words.append(word)
                segment.flags = sorted(set(segment.flags + flags))
            else:
                result.append(Segment(start=word.start, end=word.end, speaker_id=speaker, text=word.text,
                                      original_text=word.text, words=[word], flags=flags, overlap_group=group))
    # Preserve a visible slot for each untranscribed voice; don't silently drop it.
    for i, (start, end, speakers) in enumerate(windows):
        group = f"overlap-{i + 1}"
        represented = {s.speaker_id for s in result if s.overlap_group == group}
        for speaker in speakers:
            if speaker not in represented:
                result.append(Segment(start=start, end=end, speaker_id=speaker,
                                      text="[重叠发言，内容待核听]", original_text="[重叠发言，内容待核听]",
                                      flags=["overlap_unresolved", "inaudible"], overlap_group=group, source="unresolved"))
    return sorted(result, key=lambda s: (s.start, s.end, s.speaker_id)), windows


def annotate_overlaps(segments: list[Segment]):
    """Cloud outputs can already contain two independent simultaneous texts."""
    windows = overlap_windows([Turn(s.start, s.end, s.speaker_id) for s in segments])
    for i, (a, b, _) in enumerate(windows):
        for segment in segments:
            if intersection(segment.start, segment.end, a, b) > 0:
                segment.overlap_group = f"overlap-{i + 1}"
                segment.flags = sorted(set(segment.flags + ["overlap_review"]))
    return windows
