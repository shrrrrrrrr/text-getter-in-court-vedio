from court_transcript.demo import demo
from court_transcript.role_agent import configured, validate_suggestions
from tools.evaluate import score


def test_role_agent_rejects_hallucinated_or_other_speaker_evidence():
    t=demo()
    payload={'suggestions':[
        {'speaker_id':'SPEAKER_00','role':'法官','evidence':[{'segment_id':'nonexistent','quote':'我是法官'}]},
        {'speaker_id':'SPEAKER_02','role':'原告','evidence':[{'segment_id':'demo-2','quote':'我是原告'}]},
        {'speaker_id':'SPEAKER_01','role':'原告','evidence':[{'segment_id':'demo-2','quote':'不存在的精确引用'}]},
        {'speaker_id':[],'role':'法官','evidence':[]},
    ]}
    assert validate_suggestions(payload,t.speakers,t.segments,[]) == []


def test_agent_cannot_confirm_identity_or_invent_frames():
    t=demo()
    accepted=validate_suggestions({'suggestions':[{'speaker_id':'SPEAKER_01','role':'原告','role_status':'confirmed','evidence':[{'segment_id':'demo-2','quote':'我是原告'}],'frame_seconds':[1.2,9999],'reason':'自述身份'}]},t.speakers,t.segments,[1.2])
    assert accepted==['SPEAKER_01']
    p=next(p for p in t.speakers if p.id=='SPEAKER_01')
    assert p.role_status=='suggested' and p.evidence_frame_seconds==[1.2]
    assert t.segments[1].text==t.segments[1].original_text


def test_local_agent_endpoint_only(monkeypatch):
    monkeypatch.setenv('COURT_ROLE_MODEL','synthetic')
    for url in ['https://external.example/v1','http://127.0.0.1.evil.test/v1','http://user:pass@127.0.0.1/v1']:
        monkeypatch.setenv('COURT_ROLE_API_BASE',url)
        assert not configured()
    monkeypatch.setenv('COURT_ROLE_API_BASE','http://127.0.0.1:8000/v1')
    assert configured()


def test_evaluator_counts_missing_overlapping_voice():
    ref={'segments':[{'start':0,'end':2,'speaker_id':'A','text':'我，我不同意'},{'start':1,'end':3,'speaker_id':'B','text':'请听我说'}]}
    pred={'segments':[{'start':0,'end':2,'speaker_id':'S0','text':'我，我不同意'}]}
    result=score(ref,pred,{'S0':'A'})
    assert result['cer']>0 and result['speaker_aware_cer_with_manual_mapping']>0
    assert result['overlap_recall_seconds']==0
