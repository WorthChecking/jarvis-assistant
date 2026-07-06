import urllib.request
import urllib.error
import json

payload = {
    "text": "Hello, how are you?",
    "text_lang": "en",
    "ref_audio_path": r"d:\Jarvis_Assistant\Demo\vocal_vocal_ii5_3.WAV.reformatted.wav_10.wav_10.wav_0000000000_0000221440.wav",
    "prompt_text": "His son Ivan, who is also a physicist, was convicted of selling Soviet-era weapons-grade plutonium to Pakistan.",
    "prompt_lang": "en",
    "top_k": 2,
    "top_p": 0.75,
    "temperature": 0.75,
    "speed_factor": 0.9,
    "fragment_interval": 0.42,
    "text_split_method": "cut5",
    "batch_size": 1,
    "media_type": "wav",
    "streaming_mode": False,
}

req_data = json.dumps(payload).encode("utf-8")
req = urllib.request.Request(
    "http://127.0.0.1:9880/tts",
    data=req_data,
    headers={"Content-Type": "application/json"},
    method="POST",
)

try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
        print(f"SUCCESS: status={resp.status}, bytes={len(data)}")
except urllib.error.HTTPError as e:
    body = ""
    try:
        body = e.read().decode("utf-8", errors="replace")
    except Exception:
        pass
    print(f"HTTP ERROR: code={e.code}")
    print(f"BODY: {body[:2000]}")
except Exception as e:
    print(f"ERROR: {e}")
