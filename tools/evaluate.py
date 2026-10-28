"""Small reproducible evaluation CLI; compare expert reference JSON with exported JSON.

CER includes fillers/repetitions and ignores whitespace only. Speaker-aware CER requires an
explicit human-approved speaker map: unmapped predicted voices remain errors, never guessed.
"""
import argparse
import json
from pathlib import Path


def distance(a, b):
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        nxt = [i]
        for j, y in enumerate(b, 1):
            nxt.append(min(nxt[-1] + 1, row[j] + 1, row[j-1] + (x != y)))
        row = nxt
    return row[-1]


def text(segments):
    return ''.join(''.join(s['text'].split()) for s in sorted(segments, key=lambda s: (s['start'], s['end'])))


def score(reference, prediction, mapping):
    ref, hyp = reference['segments'], prediction['segments']
    expected, actual = text(ref), text(hyp)
    speaker_ids = {s['speaker_id'] for s in ref} | {mapping.get(s['speaker_id'], '__UNMAPPED__'+s['speaker_id']) for s in hyp}
    speaker_errors = 0
    for speaker in speaker_ids:
        a=text([s for s in ref if s['speaker_id']==speaker])
        b=text([s for s in hyp if mapping.get(s['speaker_id'], '__UNMAPPED__'+s['speaker_id'])==speaker])
        speaker_errors += distance(a,b)
    # Time-based overlap precision/recall on union intervals (not a DER approximation).
    boundaries=sorted({v for s in ref+hyp for v in [s['start'],s['end']]})
    tp=fp=fn=0.0
    for a,b in zip(boundaries,boundaries[1:]):
        mid=(a+b)/2
        ref_active={s['speaker_id'] for s in ref if s['start']<=mid<s['end']}
        hyp_active={s['speaker_id'] for s in hyp if s['start']<=mid<s['end'] and s.get('source')!='unresolved'}
        r,h=len(ref_active)>1,len(hyp_active)>1
        tp+=(b-a)*(r and h)
        fp+=(b-a)*(h and not r)
        fn+=(b-a)*(r and not h)
    return {'reference_characters':len(expected),'cer':distance(expected,actual)/max(1,len(expected)),
            'speaker_aware_cer_with_manual_mapping':speaker_errors/max(1,len(expected)),
            'overlap_precision_seconds':tp/(tp+fp) if tp+fp else None,
            'overlap_recall_seconds':tp/(tp+fn) if tp+fn else None,
            'unresolved_segments':sum(s.get('source')=='unresolved' for s in hyp),
            'note':'时间顺序 CER 对重叠声部排序敏感；同时检查按说话人 CER，不代表法律身份准确率'}


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('reference',type=Path)
    parser.add_argument('prediction',type=Path)
    parser.add_argument('--speaker-map',type=Path,required=True,help='JSON: predicted speaker ID -> reference person ID')
    args=parser.parse_args()
    print(json.dumps(score(json.loads(args.reference.read_text('utf-8')),json.loads(args.prediction.read_text('utf-8')),json.loads(args.speaker_map.read_text('utf-8'))),ensure_ascii=False,indent=2))
