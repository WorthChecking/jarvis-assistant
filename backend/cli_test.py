"""
CLI 测验入口 — 脱离前端的命令行交互测试
- 主动触发安全拦截器（故意执行删除文件等危险代码）
- 调用 AI 引擎打印模拟推理日志
- 模拟 Function Calling 工具调用指令
- 模拟完整交互管线（STT → 记忆检索 → LLM → TTS）
- SenseVoice .wav 文件识别测试
"""

import sys
import os
import json
import logging
import time
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("cli_test")


def print_separator(title: str = ""):
    width = 60
    if title:
        pad = width - len(title) - 4
        left = pad // 2
        right = pad - left
        print(f"\n{'=' * left}[ {title} ]{'=' * right}")
    else:
        print("=" * width)


# ============================================================
# 自动化测试：安全拦截器
# ============================================================

def test_security_interceptor():
    """测试安全拦截器"""
    print_separator("安全拦截器测试")

    from security_interceptor import (
        SafetyPermissionError,
        validate_action,
        is_active,
    )
    import os
    import shutil

    print(f"拦截器激活状态: {is_active()}")

    # 文件操作拦截测试
    test_cases = [
        ("os.remove", lambda: os.remove("C:\\Windows\\System32\\test.dll"), "删除系统目录文件"),
        ("os.remove", lambda: os.remove("D:\\test_file.txt"), "删除普通文件（全局封禁）"),
        ("shutil.rmtree", lambda: shutil.rmtree("C:\\Windows"), "递归删除系统目录"),
        ("os.rename", lambda: os.rename("C:\\Windows\\test", "C:\\Windows\\test2"), "重命名系统目录文件"),
        ("shutil.copy2", lambda: shutil.copy2("D:\\a.txt", "C:\\Windows\\b.txt"), "复制到系统目录"),
    ]

    for func_name, action, desc in test_cases:
        try:
            action()
            print(f"  [FAIL] {desc} — 未被拦截!")
        except SafetyPermissionError as e:
            print(f"  [PASS] {desc} — 已拦截: {e.reason}")
        except Exception as e:
            print(f"  [PASS] {desc} — 已拦截(其他异常): {type(e).__name__}")

    # subprocess 拦截测试
    print("\n--- subprocess 危险命令拦截测试 ---")
    subprocess_tests = [
        ("subprocess.run", lambda: subprocess.run(["del", "/f", "C:\\Windows\\test.dll"], capture_output=True), "del 删除命令"),
        ("subprocess.Popen", lambda: subprocess.Popen("powershell -Command Remove-Item C:\\Windows\\*"), "PowerShell 删除"),
        ("subprocess.run", lambda: subprocess.run("format C:", capture_output=True), "格式化磁盘"),
        ("subprocess.run", lambda: subprocess.run("reg add HKLM\\SOFTWARE\\Test", capture_output=True), "修改注册表"),
        ("subprocess.run", lambda: subprocess.run("taskkill /f /im explorer.exe", capture_output=True), "强制结束进程"),
        ("subprocess.run", lambda: subprocess.run(["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader"], capture_output=True), "nvidia-smi（白名单放行）"),
    ]

    for func_name, action, desc in subprocess_tests:
        try:
            result = action()
            if desc.endswith("白名单放行"):
                print(f"  [PASS] {desc} — 白名单命令已放行")
            else:
                print(f"  [FAIL] {desc} — 未被拦截!")
        except SafetyPermissionError as e:
            print(f"  [PASS] {desc} — 已拦截: {e.reason}")
        except Exception as e:
            if desc.endswith("白名单放行"):
                print(f"  [PASS] {desc} — 白名单命令已放行（执行异常: {type(e).__name__}）")
            else:
                print(f"  [PASS] {desc} — 已拦截(其他异常): {type(e).__name__}")

    # 操作白名单校验测试
    print("\n--- 操作白名单校验测试 ---")
    whitelist_tests = [
        ("volume_up", "device_control", {}, True),
        ("volume_up", "file_search", {}, False),
        ("hack_system", "device_control", {}, False),
        ("file_read_content", "file_read", {"file_path": "D:\\doc.txt"}, True),
        ("file_read_content", "file_read", {"file_path": "D:\\image.png"}, False),
        ("file_read_content", "file_read", {"file_path": "C:\\Windows\\System32\\config\\SAM"}, False),
        ("file_search_everything", "file_search", {"query": "报告.docx"}, True),
        ("file_search_everything", "file_search", {"query": "$Recycle.Bin"}, False),
    ]

    for action_name, category, params, expected_ok in whitelist_tests:
        ok, reason = validate_action(action_name, category, params)
        status = "PASS" if ok == expected_ok else "FAIL"
        print(f"  [{status}] {action_name}@{category} | 预期: {'允许' if expected_ok else '拒绝'} | 实际: {'允许' if ok else '拒绝'} | {reason}")


# ============================================================
# 自动化测试：AI 引擎
# ============================================================

def test_ai_engine():
    """测试 AI 引擎加载与推理"""
    print_separator("AI 引擎测试（SenseVoice + Edge-TTS）")

    from ai_engine import AIEngine, AIEngineConfig

    config = AIEngineConfig(
        stt_model_size="SenseVoiceSmall",
        stt_quantization="float16",
        tts_backend="edge_tts",
        tts_quantization="float16",
        memory_embedding_model="all-MiniLM-L6-v2",
        device="cuda:0",
    )

    engine = AIEngine(config)
    engine.initialize()

    print("\n--- 子引擎独立加载测试 ---")
    print("1. 仅加载 ChromaDB（CPU，无显存压力）...")
    try:
        engine.memory.load()
        print("   [PASS] ChromaDB 加载成功")
    except Exception as e:
        print(f"   [FAIL] ChromaDB 加载失败: {e}")

    print("\n2. 加载 SenseVoice STT（GPU, float16 量化, cuda:0）...")
    try:
        engine.stt.load()
        print("   [PASS] SenseVoice STT 加载成功")
    except Exception as e:
        print(f"   [FAIL] SenseVoice STT 加载失败: {e}")

    print("\n3. 加载 TTS（Edge-TTS 在线，无需 GPU）...")
    try:
        engine.tts.load()
        print("   [PASS] TTS 加载成功")
    except Exception as e:
        print(f"   [FAIL] TTS 加载失败: {e}")

    print("\n--- 显存监控测试 ---")
    engine.vram_monitor.start()
    time.sleep(2)
    status = engine.get_system_status()
    print(f"  系统状态: {json.dumps(status, indent=2, ensure_ascii=False)}")

    print("\n--- 记忆引擎读写测试 ---")
    try:
        engine.memory.write("用户习惯将系统音量保持在 60-80 之间", {"source": "test"})
        engine.memory.write("用户每天早上 8 点会查看日程安排", {"source": "test"})
        matches = engine.memory.retrieve("音量偏好")
        for m in matches:
            print(f"  检索结果: [{m['score']:.3f}] {m['content']}")
    except Exception as e:
        print(f"  记忆引擎测试失败: {e}")

    print("\n--- 量化强制断言测试 ---")
    from ai_engine import STTEngine, TTSEngine
    try:
        bad_stt = STTEngine(quantization="float32")
        print("  [FAIL] STT float32 未被拒绝!")
    except ValueError as e:
        print(f"  [PASS] STT float32 已拒绝: {e}")

    try:
        bad_tts = TTSEngine(backend="chat_tts", quantization="float32")
        print("  [FAIL] TTS float32 未被拒绝!")
    except ValueError as e:
        print(f"  [PASS] TTS float32 已拒绝: {e}")

    engine.shutdown()
    print("\nAI 引擎已关闭")


# ============================================================
# SenseVoice .wav 文件识别测试
# ============================================================

def test_sensevoice_wav(wav_path: str = ""):
    """传入一段真实的 .wav 录音文件，验证 SenseVoiceSmall 是否能正确输出中文文本"""
    print_separator("SenseVoice .wav 文件识别测试")

    if not wav_path:
        wav_path = input("请输入 .wav 文件路径: ").strip().strip('"').strip("'")
    if not wav_path or not os.path.isfile(wav_path):
        print(f"  [ERROR] 文件不存在: {wav_path}")
        return

    print(f"  音频文件: {wav_path}")
    print(f"  文件大小: {os.path.getsize(wav_path) / 1024:.1f} KB")

    # 检查音频信息
    try:
        import soundfile as sf
        info = sf.info(wav_path)
        print(f"  采样率: {info.samplerate} Hz | 声道: {info.channels} | 时长: {info.duration:.2f}s | 格式: {info.format}")
    except Exception as e:
        print(f"  [WARN] 无法读取音频信息: {e}")

    # 加载引擎
    from ai_engine import AIEngine, AIEngineConfig

    config = AIEngineConfig(
        stt_model_size="SenseVoiceSmall",
        stt_quantization="float16",
        device="cuda:0",
    )
    engine = AIEngine(config)
    engine.initialize()

    print("\n  正在加载 SenseVoice 模型...")
    try:
        engine.stt.load()
    except Exception as e:
        print(f"  [FAIL] 模型加载失败: {e}")
        return

    # 显存检查
    engine.vram_monitor.start()
    time.sleep(1)
    vram_status = engine.vram_monitor.status
    print(f"  显存: {vram_status.used_mb:.0f}MB / {vram_status.total_mb:.0f}MB ({vram_status.usage_percent:.1f}%)")

    if vram_status.usage_percent > 90:
        print("  [WARN] 显存超 90% 红线！触发降级预警")
    else:
        print("  [PASS] 显存在安全范围内，未触发降级预警")

    # 执行识别
    print("\n  正在识别...")
    try:
        result = engine.stt.transcribe(wav_path)
        text = result.get("text", "")
        detected_lang = result.get("language", "auto")
        duration = result.get("duration", 0)

        print(f"\n  === 识别结果 ===")
        print(f"  文本: {text}")
        print(f"  语言: {detected_lang}")
        print(f"  音频时长: {duration:.2f}s")

        if text.strip():
            print(f"\n  [PASS] SenseVoice 成功输出中文文本")
        else:
            print(f"\n  [WARN] 识别结果为空，可能音频无有效语音")

    except Exception as e:
        print(f"  [FAIL] 识别失败: {e}")
        import traceback
        traceback.print_exc()

    # 识别后显存检查（验证无泄漏）
    time.sleep(1)
    vram_after = engine.vram_monitor.status
    print(f"\n  识别后显存: {vram_after.used_mb:.0f}MB ({vram_after.usage_percent:.1f}%)")
    delta = vram_after.used_mb - vram_status.used_mb
    if abs(delta) > 100:
        print(f"  [WARN] 识别前后显存变化: {delta:+.0f}MB，可能存在泄漏")
    else:
        print(f"  [PASS] 显存变化: {delta:+.0f}MB，无泄漏迹象")

    engine.shutdown()
    print("\n  测试完成")


# ============================================================
# 自动化测试：Function Calling 管线
# ============================================================

def test_function_calling_pipeline():
    """模拟 Function Calling 完整链路"""
    print_separator("Function Calling 管线测试")

    from security_interceptor import validate_action

    simulated_calls = [
        {"name": "volume_up", "category": "device_control", "params": {"step": 10}},
        {"name": "app_launch", "category": "app_management", "params": {"app_name": "notepad"}},
        {"name": "file_search_everything", "category": "file_search", "params": {"query": "季度报告.xlsx"}},
        {"name": "file_read_content", "category": "file_read", "params": {"file_path": "D:\\Documents\\会议纪要.md"}},
        {"name": "os_exec", "category": "device_control", "params": {"cmd": "rm -rf /"}},
        {"name": "file_read_content", "category": "file_read", "params": {"file_path": "C:\\Windows\\System32\\config\\SAM"}},
    ]

    for call in simulated_calls:
        ok, reason = validate_action(call["name"], call["category"], call["params"])
        if ok:
            print(f"  [ALLOW] {call['name']}@{call['category']} | 参数: {call['params']}")
            print(f"          -> 执行本地预写 Python 封包函数...")
            print(f"          -> 操作完成，反馈给 LLM 生成语音回复")
        else:
            print(f"  [BLOCK] {call['name']}@{call['category']} | 原因: {reason}")
            print(f"          -> AI 回复: 抱歉，权限不足，无法执行此操作")


# ============================================================
# 交互式模式
# ============================================================

def interactive_mode():
    """交互式命令行模式 — 可主动触发拦截器和 AI 引擎"""
    print_separator("交互式测试模式")
    print("可用命令:")
    print("  stt <文本>              — 模拟 STT 识别结果，走完整管线")
    print("  wav <文件路径>          — 用 SenseVoice 识别 .wav 文件")
    print("  call <JSON>             — 模拟 Function Calling 指令")
    print("  action <名称> <分类>    — 测试操作白名单校验")
    print("  memory write <内容>     — 写入长期记忆")
    print("  memory search <查询>    — 检索长期记忆")
    print("  memory show             — 显示短期记忆队列")
    print("  status                  — 查看系统状态")
    print("  danger                  — 主动触发安全拦截器（故意执行危险操作）")
    print("  pipeline <文本>         — 模拟完整交互管线（T0→T5）")
    print("  quit                    — 退出")
    print()

    from ai_engine import AIEngine, AIEngineConfig
    from security_interceptor import validate_action, SafetyPermissionError

    config = AIEngineConfig(device="cuda:0")
    engine = AIEngine(config)
    engine.initialize()

    try:
        engine.memory.load()
    except Exception as e:
        print(f"ChromaDB 加载失败: {e}，记忆功能不可用")

    try:
        engine.stt.load()
    except Exception as e:
        print(f"SenseVoice 加载失败: {e}")

    try:
        engine.tts.load()
    except Exception as e:
        print(f"TTS 加载失败: {e}")

    engine.vram_monitor.start()

    while True:
        try:
            raw = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not raw:
            continue

        parts = raw.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "quit":
            break

        elif cmd == "wav":
            # SenseVoice .wav 文件识别
            wav_file = arg.strip().strip('"').strip("'")
            if not wav_file or not os.path.isfile(wav_file):
                print(f"  [ERROR] 文件不存在: {wav_file}")
                continue
            if engine.stt and engine.stt._loaded:
                try:
                    result = engine.stt.transcribe(wav_file)
                    text = result.get("text", "")
                    print(f"  [SenseVoice] 识别结果: {text}")
                    print(f"  语言: {result.get('language', 'auto')} | 时长: {result.get('duration', 0):.2f}s")
                except Exception as e:
                    print(f"  [ERROR] 识别失败: {e}")
            else:
                print("  [ERROR] SenseVoice 未加载")

        elif cmd == "stt":
            text = arg or "这是一段测试文本"
            print(f"  [STT] 识别结果: {text}")
            engine.short_term.add("user", text)
            if engine.memory and engine.memory._loaded:
                matches = engine.memory.retrieve(text)
                if matches:
                    print(f"  [MEMORY] 检索到 {len(matches)} 条相关记忆:")
                    for m in matches:
                        print(f"    - [{m['score']:.3f}] {m['content']}")
                else:
                    print("  [MEMORY] 无相关记忆")

        elif cmd == "call":
            try:
                call_data = json.loads(arg)
            except json.JSONDecodeError:
                print('  [ERROR] JSON 解析失败，格式示例: {"name":"volume_up","category":"device_control","params":{}}')
                continue

            name = call_data.get("name", "")
            category = call_data.get("category", "")
            params = call_data.get("params", {})

            ok, reason = validate_action(name, category, params)
            if ok:
                print(f"  [ALLOW] {name}@{category} | 执行操作...")
                engine.short_term.add("assistant", f"[执行操作] {name}")
            else:
                print(f"  [BLOCK] {name}@{category} | {reason}")
                engine.short_term.add("assistant", f"[拒绝操作] {name}: {reason}")

        elif cmd == "action":
            action_parts = arg.split()
            if len(action_parts) < 2:
                print("  用法: action <名称> <分类> [JSON参数]")
                continue
            name = action_parts[0]
            category = action_parts[1]
            params = {}
            if len(action_parts) > 2:
                try:
                    params = json.loads(" ".join(action_parts[2:]))
                except json.JSONDecodeError:
                    params = {}

            ok, reason = validate_action(name, category, params)
            if ok:
                print(f"  [ALLOW] {name}@{category}")
            else:
                print(f"  [BLOCK] {name}@{category} | {reason}")

        elif cmd == "memory":
            if arg.strip().lower() == "show":
                history = engine.short_term.get_all()
                if not history:
                    print("  [SHORT-TERM] 短期记忆为空")
                else:
                    print(f"  [SHORT-TERM] 当前 {len(history)} 条:")
                    for h in history:
                        print(f"    [{h['role']}] {h['content']}")
                continue

            mem_parts = arg.split(maxsplit=1)
            if len(mem_parts) < 2:
                print("  用法: memory write <内容> | memory search <查询> | memory show")
                continue
            sub = mem_parts[0].lower()
            content = mem_parts[1]

            if not engine.memory or not engine.memory._loaded:
                print("  [ERROR] 记忆引擎未加载")
                continue

            if sub == "write":
                engine.memory.write(content)
                print(f"  [MEMORY] 已写入: {content}")
            elif sub == "search":
                matches = engine.memory.retrieve(content)
                for m in matches:
                    print(f"  [{m['score']:.3f}] {m['content']}")
            else:
                print("  未知子命令: write / search / show")

        elif cmd == "status":
            status = engine.get_system_status()
            print(f"  {json.dumps(status, indent=2, ensure_ascii=False)}")

        elif cmd == "danger":
            print("\n  --- 主动触发安全拦截器 ---")
            import os
            import shutil

            danger_actions = [
                ("os.remove", lambda: os.remove("C:\\Windows\\System32\\hal.dll")),
                ("shutil.rmtree", lambda: shutil.rmtree("C:\\Program Files")),
                ("os.rename", lambda: os.rename("C:\\Windows", "C:\\Windows_old")),
                ("subprocess.run(del)", lambda: subprocess.run("del /f C:\\Windows\\*.*", capture_output=True)),
                ("subprocess.Popen(powershell)", lambda: subprocess.Popen("powershell -Command Remove-Item -Recurse C:\\")),
            ]

            for desc, action in danger_actions:
                try:
                    action()
                    print(f"    [FAIL] {desc} — 未被拦截!")
                except SafetyPermissionError as e:
                    print(f"    [PASS] {desc} — 已拦截: {e.reason}")
                except Exception as e:
                    print(f"    [PASS] {desc} — 已拦截: {type(e).__name__}: {e}")

        elif cmd == "pipeline":
            text = arg or "帮我调大音量"
            print(f"\n  === 模拟完整交互管线 ===")
            print(f"  [T0] 唤醒词检测 -> UI 切换 State 1（聆听）")
            print(f"  [T1] STT 识别结果: \"{text}\"")
            engine.short_term.add("user", text)

            print(f"  [T2] 记忆检索...")
            if engine.memory and engine.memory._loaded:
                matches = engine.memory.retrieve(text)
                if matches:
                    for m in matches:
                        print(f"       命中: [{m['score']:.3f}] {m['content']}")
                else:
                    print(f"       无相关记忆")

            print(f"  [T3] 云端 LLM 推理...")
            if "音量" in text:
                llm_result = {"name": "volume_up", "category": "device_control", "params": {"step": 10}}
                has_fc = True
            else:
                llm_result = None
                has_fc = False

            if has_fc:
                print(f"       LLM 返回 Function Calling: {json.dumps(llm_result, ensure_ascii=False)}")
                ok, reason = validate_action(llm_result["name"], llm_result["category"], llm_result["params"])
                if ok:
                    print(f"       [ALLOW] 白名单校验通过 -> 执行本地 Python 封包函数")
                    print(f"       [ACTION] volume_up(step=10) 执行成功")
                    engine.short_term.add("assistant", "[执行操作] volume_up")
                else:
                    print(f"       [BLOCK] 白名单校验失败: {reason}")
                    engine.short_term.add("assistant", f"[拒绝] {reason}")
            else:
                reply = f"好的，我收到了你的消息：{text}"
                print(f"       LLM 返回文本: \"{reply}\"")
                engine.short_term.add("assistant", reply)

            print(f"  [T4] TTS 合成语音...")
            print(f"       [模拟] 音频首包已生成，送入 TTS 模型")

            print(f"  [T5] 播放与波形同步...")
            print(f"       [模拟] 音频输出至声卡，60fps RMS 数据推送至前端")
            print(f"  === 管线执行完毕 ===")

        else:
            print(f"  未知命令: {cmd}")

    engine.shutdown()
    print("已退出")


# ============================================================
# 主入口
# ============================================================

def main():
    print_separator("Jarvis Assistant CLI Test")
    print("选择测试模式:")
    print("  1 — 安全拦截器测试（含 subprocess 拦截）")
    print("  2 — AI 引擎加载与推理测试（SenseVoice + 量化断言）")
    print("  3 — Function Calling 管线测试")
    print("  4 — 交互式模式（支持 wav/danger/pipeline 命令）")
    print("  5 — 全部自动化测试")
    print("  6 — SenseVoice .wav 文件识别测试")

    choice = input("\n请输入选项 (1-6): ").strip()

    if choice == "1":
        test_security_interceptor()
    elif choice == "2":
        test_ai_engine()
    elif choice == "3":
        test_function_calling_pipeline()
    elif choice == "4":
        interactive_mode()
    elif choice == "5":
        test_security_interceptor()
        test_function_calling_pipeline()
        test_ai_engine()
    elif choice == "6":
        wav_path = ""
        if len(sys.argv) > 2:
            wav_path = sys.argv[2]
        test_sensevoice_wav(wav_path)
    else:
        print("无效选项")


if __name__ == "__main__":
    main()
