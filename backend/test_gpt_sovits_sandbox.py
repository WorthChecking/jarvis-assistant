import sys, os, time, json, urllib.request, urllib.parse, wave, io

GPT_SOVITS_DIR = r"d:\GPT-SoVITS"
API_URL = "http://127.0.0.1:9880"
REF_AUDIO = r"d:\Jarvis_Assistant\Demo\vocal_vocal_ii5_3.WAV.reformatted.wav_10.wav_10.wav_0000000000_0000221440.wav"
REF_TEXT = "His son Ivan, who is also a physicist, was convicted of selling Soviet-era weapons-grade plutonium to Pakistan."

print("=" * 60)
print("GPT-SoVITS API 沙箱测试")
print("=" * 60)

# 1. 检查 API 是否已在运行
print("\n[1] 检查 API 是否已在运行...")
api_running = False
try:
    req = urllib.request.Request(f"{API_URL}/tts?text=test&text_lang=en&ref_audio_path={urllib.parse.quote(REF_AUDIO)}&prompt_lang=en&prompt_text=hello", method="GET")
    urllib.request.urlopen(req, timeout=5)
    print("API 已在运行")
    api_running = True
except Exception as e:
    print(f"API 未运行: {e}")

# 2. 启动 API 服务
if not api_running:
    print("\n[2] 启动 GPT-SoVITS API 服务...")
    import subprocess as sp

    runtime_python = os.path.join(GPT_SOVITS_DIR, "runtime", "python.exe")
    api_script = os.path.join(GPT_SOVITS_DIR, "api_v2.py")
    config_file = os.path.join(GPT_SOVITS_DIR, "GPT_SoVITS", "configs", "tts_infer.yaml")

    print(f"Runtime: {runtime_python}")
    print(f"Script: {api_script}")
    print(f"Config: {config_file}")
    print(f"Runtime exists: {os.path.exists(runtime_python)}")
    print(f"Script exists: {os.path.exists(api_script)}")
    print(f"Config exists: {os.path.exists(config_file)}")

    # 检查模型文件
    gpt_model = os.path.join(GPT_SOVITS_DIR, "GPT_weights_v2Pro", "J.a.r.v.i.s-e15.ckpt")
    sovits_model = os.path.join(GPT_SOVITS_DIR, "SoVITS_weights_v2Pro", "J.a.r.v.i.s_e8_s400.pth")
    print(f"GPT model exists: {os.path.exists(gpt_model)} ({gpt_model})")
    print(f"SoVITS model exists: {os.path.exists(sovits_model)} ({sovits_model})")

    # 检查参考音频
    print(f"Ref audio exists: {os.path.exists(REF_AUDIO)} ({REF_AUDIO})")

    proc = sp.Popen(
        [runtime_python, api_script, "-a", "127.0.0.1", "-p", "9880", "-c", config_file],
        cwd=GPT_SOVITS_DIR,
        stdout=sp.PIPE,
        stderr=sp.STDOUT,
        creationflags=0x08000000,
    )

    print(f"API 进程已启动 | PID={proc.pid}")
    print("等待 API 就绪（最多 120 秒）...")

    # 等待就绪
    for i in range(120):
        line = proc.stdout.readline().decode("utf-8", errors="replace").strip() if proc.stdout else ""
        if line:
            print(f"  [{i}s] {line[:200]}")

        try:
            payload = json.dumps({
                "text": "Hello, I am JARVIS.",
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
            }).encode("utf-8")

            req = urllib.request.Request(
                f"{API_URL}/tts",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                wav_bytes = resp.read()

            if wav_bytes and len(wav_bytes) > 44:
                print(f"\n[3] 合成成功！等待 {i+1} 秒")
                print(f"  WAV 字节数: {len(wav_bytes)}")

                # 解析 WAV
                wav_io = io.BytesIO(wav_bytes)
                with wave.open(wav_io, "rb") as wf:
                    sr = wf.getframerate()
                    ch = wf.getnchannels()
                    sw = wf.getsampwidth()
                    frames = wf.getnframes()
                    pcm = wf.readframes(frames)

                print(f"  采样率: {sr}")
                print(f"  声道数: {ch}")
                print(f"  采样宽度: {sw} bytes")
                print(f"  PCM 字节数: {len(pcm)}")
                print(f"  时长: {len(pcm) / (sr * ch * sw):.2f}s")

                # 保存测试文件
                test_out = r"d:\Jarvis_Assistant\backend\test_gpt_sovits_output.wav"
                with open(test_out, "wb") as f:
                    f.write(wav_bytes)
                print(f"  已保存测试文件: {test_out}")

                print("\n测试 PASS")
                break
            else:
                print(f"  [{i}s] 返回数据过短: {len(wav_bytes) if wav_bytes else 0}")
        except Exception as e:
            if i % 10 == 0:
                print(f"  [{i}s] 等待中... ({e})")
            time.sleep(1)
    else:
        print("\n测试 FAIL: API 启动超时")
        # 读取剩余输出
        remaining = proc.stdout.read(2000).decode("utf-8", errors="replace") if proc.stdout else ""
        if remaining:
            print(f"剩余输出: {remaining[:500]}")

    # 终止 API 进程
    print("\n[4] 终止 API 进程...")
    proc.terminate()
    try:
        proc.wait(timeout=5)
        print("API 进程已终止")
    except Exception:
        proc.kill()
        print("API 进程已强制终止")

print("\n" + "=" * 60)
print("测试完成")
print("=" * 60)
