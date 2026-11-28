"""Li voice: Kokoro 82M (open weights, Apache 2.0) speaking on the box's own CPU.
Listens on 127.0.0.1 only. li-auth checks the sign-in and passes text here."""
import io, os, re, threading, wave
import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from kokoro_onnx import Kokoro

HOME = os.environ.get('LI_TTS_HOME', '/opt/li-tts')
VOICES = {'af_heart', 'am_michael', 'bf_emma', 'bm_george', 'af_bella', 'am_fenrir'}
kk = Kokoro(os.path.join(HOME, 'kokoro-v1.0.onnx'), os.path.join(HOME, 'voices-v1.0.bin'))
lock = threading.Lock()
app = FastAPI()

class Say(BaseModel):
    text: str
    voice: str = 'af_heart'
    speed: float = 1.0

def clean(t: str) -> str:
    t = re.sub(r'```[\s\S]*?```', ' I put the code in the chat. ', t)
    t = re.sub(r'`([^`]*)`', r'\1', t)
    t = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', t)
    t = re.sub(r'https?://\S+', 'a link', t)
    t = re.sub(r'\[\d+(?:,\s*\d+)*\]', '', t)
    t = re.sub(r'[*_#>|~]+', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()[:600]

@app.get('/health')
def health():
    return {'ok': True, 'voices': sorted(VOICES)}

@app.post('/tts')
def tts(s: Say):
    text = clean(s.text)
    if not re.search(r'[A-Za-z0-9]', text):
        raise HTTPException(400, 'nothing to say')
    voice = s.voice if s.voice in VOICES else 'af_heart'
    speed = min(1.4, max(0.7, s.speed))
    with lock:
        audio, sr = kk.create(text, voice=voice, speed=speed, lang='en-gb' if voice[0] == 'b' else 'en-us')
    pcm = (np.clip(audio, -1, 1) * 32767).astype('<i2').tobytes()
    b = io.BytesIO()
    with wave.open(b, 'wb') as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes(pcm)
    return Response(b.getvalue(), media_type='audio/wav', headers={'Cache-Control': 'no-store'})

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=int(os.environ.get('LI_TTS_PORT', '3012')), log_level='warning')
