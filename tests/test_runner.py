import time
from threading import Event

from court_transcript.config import Settings
from court_transcript.demo import demo
from court_transcript.media import Cancelled
from court_transcript.runner import Runner
from court_transcript.store import Store


def test_worker_success_and_original_snapshot(tmp_path, monkeypatch):
    import court_transcript.runner as module
    store=Store(tmp_path)
    task=demo()
    task.is_demo=False
    task.state='queued'
    store.save(task)
    directory=store.directory(task.id)
    (directory/'input.media').write_bytes(b'synthetic')
    monkeypatch.setattr(module,'audio',lambda source,target,*args:target.write_bytes(b'synthetic-audio'))
    monkeypatch.setattr(module,'command',lambda *a,**kw:b'ffmpeg fixture')
    runner=Runner(store,Settings(data_dir=tmp_path))
    monkeypatch.setattr(runner.local,'run',lambda *args:(task.segments,[],{'asr_model':'synthetic-fixture'}))
    runner.submit(task.id)
    for _ in range(100):
        if store.get(task.id).state=='completed':break
        time.sleep(.02)
    result=store.get(task.id)
    runner.shutdown()
    assert result.state=='completed'
    assert (directory/'original-result.json').exists()
    assert result.manifest['asr_model']=='synthetic-fixture'
    assert all(p.role_status!='confirmed' for p in result.speakers)


def test_restart_requires_manual_resume(tmp_path):
    store=Store(tmp_path)
    task=demo()
    task.state='running'
    store.save(task)
    runner=Runner(store,Settings(data_dir=tmp_path))
    runner.recover()
    assert store.get(task.id).state=='interrupted'
    assert not runner.events
    runner.shutdown()


def test_cancelled_subprocess_exits_and_leaves_no_live_queue_entry(tmp_path):
    store = Store(tmp_path)
    task = demo()
    task.state, task.is_demo = 'queued', False
    store.save(task)
    (store.directory(task.id) / 'input.media').write_bytes(b'synthetic-invalid-media')
    runner = Runner(store, Settings(data_dir=tmp_path, worker_subprocess=True))
    runner.submit(task.id)
    assert runner.cancel(task.id)
    for _ in range(100):
        if task.id not in runner.events:
            break
        time.sleep(.1)
    runner.shutdown()
    assert task.id not in runner.events
    assert store.get(task.id).state == 'cancelled'
