import shutil
import subprocess

import pytest

from court_transcript.media import audio, frame, preview, probe

pytestmark = pytest.mark.skipif(not shutil.which('ffmpeg'), reason='FFmpeg unavailable')


@pytest.mark.parametrize('container,video_codec,audio_codec', [('mp4','mpeg4','aac'),('mkv','mpeg4','aac'),('mov','mpeg4','aac'),('avi','mpeg4','mp3'),('webm','libvpx','libopus'),('flv','flv','mp3')])
def test_actual_container_normalization(tmp_path,container,video_codec,audio_codec):
    path=tmp_path/f'fixture.{container}'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=c=0x132a49:s=160x90:r=5:d=1','-f','lavfi','-i','sine=frequency=400:duration=1','-c:v',video_codec,'-c:a',audio_codec,'-shortest',str(path)],check=True)
    meta=probe(path)
    assert meta['has_video'] and len(meta['audio_streams'])==1
    target=tmp_path/'audio.wav'
    audio(path,target)
    assert probe(target)['audio_streams'][0]['sample_rate']=='16000'
    if container=='mkv':
        preview(path,tmp_path/'preview.mp4')
        frame(path,tmp_path/'frame.jpg',.3)
        assert (tmp_path/'frame.jpg').stat().st_size>100


def test_no_audio_and_playlist_rejected(tmp_path):
    path=tmp_path/'silent.mp4'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','color=s=160x90:d=0.5','-c:v','mpeg4',str(path)],check=True)
    with pytest.raises(ValueError,match='未发现音轨'):
        probe(path)
    playlist=tmp_path/'disguised.mp4'
    playlist.write_text('#EXTM3U\n#EXT-X-TARGETDURATION:10\nhttp://127.0.0.1:1/private\n','utf-8')
    with pytest.raises(RuntimeError):
        probe(playlist)
