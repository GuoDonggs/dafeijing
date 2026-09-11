import sys, time, pathlib, tempfile
sys.path.insert(0, str(pathlib.Path.cwd()))
from voice_agent import speech
from voice_agent.console import Console

played = []
def fake_play(samples, rate, **kw):
    played.append(int(samples.size))
    return True
speech.audio_io.play = fake_play

tmp = pathlib.Path(tempfile.mkdtemp()) / 'c.yaml'
tmp.write_text('tts:\n  engine: chattts\n  voice: seed1111\n', encoding='utf-8')
c = Console(tmp)
print('试听文本:', c.AUDITION_TEXT)
print('音色:', c.voices_payload()['current_label'])
print('返回:', c.audition_voice('seed1111'))
st = None
for _ in range(120):
    time.sleep(0.5)
    st = c.audition_status()
    if st['state'] in ('done', 'error'):
        break
print('最终:', st['state'], '| 错误:', st.get('error') or '无')
print('播放块数:', len(played), played)