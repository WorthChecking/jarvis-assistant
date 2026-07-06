import json, urllib.request, urllib.parse, sys
sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)

API_URL = "http://127.0.0.1:9880"
REF_AUDIO = r"d:\Jarvis_Assistant\Demo\vocal_vocal_ii5_3.WAV.reformatted.wav_10.wav_10.wav_0000000000_0000221440.wav"
REF_TEXT = "His son Ivan, who is also a physicist, was convicted of selling Soviet-era weapons-grade plutonium to Pakistan."

import os
print(f"Ref audio exists: {os.path.exists(REF_AUDIO)}")
print(f"Ref audio size: {os.path.getsize(REF_AUDIO) if os.path.exists(REF_AUDIO) else 'N/A'}")

payload = {
    "text": "Hello, I am JARVIS. How can I help you today?",
    "text_lang": "en",
    "ref_audio_path": REF_AUDIO,
    "prompt_text": REF_TEXT,
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

print(f"Payload: {json.dumps(payload, indent=2)}")

try:
    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{API_URL}/tts",
        data=req_data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    print("Sending request...")
    with urllib.request.urlopen(req, timeout=120) as resp:
        wav_bytes = resp.read()
    print(f"Success! WAV bytes: {len(wav_bytes)}")

    import wave, io
    wav_io = io.BytesIO(wav_bytes)
    with wave.open(wav_io, "rb") as wf:
        print(f"Sample rate: {wf.getframerate()}")
        print(f"Channels: {wf.getnchannels()}")
        print(f"Duration: {wf.getnframes() / wf.getframerate():.2f}s")

    with open(r"d:\Jarvis_Assistant\backend\test_gpt_sovits_output.wav", "wb") as f:
        f.write(wav_bytes)
    print("Saved to test_gpt_sovits_output.wav")
    print("PASS")

except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code} {e.reason}")
    error_body = e.read().decode("utf-8", errors="replace")
    print(f"Error body: {error_body}")
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
