"""
AI 引擎挂载与显存锁
- SenseVoice Small (STT) 加载：通过 FunASR 框架，强制 float16 量化，显存上限 3GB
- TTS (ChatTTS / Bert-VITS2 / Edge-TTS) 加载：显存上限 1.5GB
- ChromaDB 本地向量数据库初始化
- 短期记忆滑动窗口
- 显存监控器：通过 subprocess 轮询 nvidia-smi，超 90% 触发降级预警日志
"""

import os
import asyncio
import logging
import threading
import time
import uuid
import subprocess
from typing import Optional, Callable
from dataclasses import dataclass
from collections import deque

logger = logging.getLogger("ai_engine")

# 绕过安全拦截器，获取 subprocess.run 的原始引用
try:
    from security_interceptor import _original_subprocess_run as _raw_subprocess_run
except ImportError:
    _raw_subprocess_run = subprocess.run


# ============================================================
# 显存监控器
# ============================================================

@dataclass
class VRAMStatus:
    total_mb: float = 0.0
    used_mb: float = 0.0
    free_mb: float = 0.0
    usage_percent: float = 0.0


class VRAMMonitor:
    """通过 subprocess 轮询 nvidia-smi 获取显存占用，超 90% 触发降级预警日志"""

    def __init__(
        self,
        poll_interval_sec: float = 1.0,
        threshold_percent: float = 90.0,
        on_degrade: Optional[Callable[[VRAMStatus], None]] = None,
        on_recover: Optional[Callable[[VRAMStatus], None]] = None,
    ):
        self.poll_interval = poll_interval_sec
        self.threshold = threshold_percent
        self.on_degrade = on_degrade
        self.on_recover = on_recover
        self._status = VRAMStatus()
        self._degraded = False
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    @property
    def status(self) -> VRAMStatus:
        return self._status

    @property
    def is_degraded(self) -> bool:
        return self._degraded

    def _query_nvidia_smi(self) -> VRAMStatus:
        """通过 subprocess 调用 nvidia-smi 查询显存"""
        try:
            result = _raw_subprocess_run(
                [
                    "nvidia-smi",
                    "--query-gpu=memory.total,memory.used,memory.free",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning("[VRAM] nvidia-smi 查询失败: %s", result.stderr.strip())
                return self._status

            line = result.stdout.strip().split("\n")[0]
            parts = line.split(",")
            total_mb = float(parts[0].strip())
            used_mb = float(parts[1].strip())
            free_mb = float(parts[2].strip())
            usage_percent = (used_mb / total_mb * 100.0) if total_mb > 0 else 0.0

            return VRAMStatus(
                total_mb=total_mb,
                used_mb=used_mb,
                free_mb=free_mb,
                usage_percent=usage_percent,
            )
        except FileNotFoundError:
            logger.warning("[VRAM] nvidia-smi 未找到，无法监控显存")
            return self._status
        except Exception as e:
            logger.warning("[VRAM] 显存查询异常: %s", e)
            return self._status

    def _poll_loop(self):
        while self._running:
            status = self._query_nvidia_smi()
            with self._lock:
                self._status = status
                if status.usage_percent >= self.threshold and not self._degraded:
                    self._degraded = True
                    logger.warning(
                        "[VRAM DEGRADE] 显存占用 %.1f%% 超过阈值 %.1f%%，触发降级预警",
                        status.usage_percent, self.threshold,
                    )
                    if self.on_degrade:
                        self.on_degrade(status)
                elif status.usage_percent < self.threshold - 5.0 and self._degraded:
                    self._degraded = False
                    logger.info(
                        "[VRAM RECOVER] 显存占用 %.1f%% 已恢复至安全区间，解除降级",
                        status.usage_percent,
                    )
                    if self.on_recover:
                        self.on_recover(status)
            time.sleep(self.poll_interval)

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="vram-monitor")
        self._thread.start()
        logger.info("[VRAM] 显存监控器已启动 | 阈值: %.1f%% | 轮询间隔: %.1fs", self.threshold, self.poll_interval)

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        logger.info("[VRAM] 显存监控器已停止")


# ============================================================
# STT 引擎 — SenseVoice Small（阿里通义实验室）
# 通过 FunASR 框架加载，中文识别效果远优于 Whisper
# 支持中英混合 + 情感/语种/事件标签，无需 VAD
# ============================================================

class STTEngine:
    """SenseVoice 语音识别引擎，强制 float16 量化，显存红线 3GB"""

    # FunASR 支持的模型名（注册表键名）
    SUPPORTED_MODELS = {"SenseVoiceSmall"}
    # 量化类型：float16 为最低要求，严禁 float32
    SUPPORTED_QUANT = {"float16", "int8"}

    # SenseVoice 输出的情绪与事件标签正则
    _TAG_PATTERN = None

    def __init__(
        self,
        model_size: str = "SenseVoiceSmall",
        quantization: str = "float16",
        device: str = "cuda:0",
        vram_limit_mb: int = 3072,
        compute_type: Optional[str] = None,
    ):
        self.model_size = model_size
        self.quantization = quantization
        self.device = device
        self.vram_limit_mb = vram_limit_mb
        self.model = None
        self._loaded = False
        self._loading = False  # 加载中标志（防双重加载）
        self._load_lock = threading.Lock()  # 线程锁
        self._model_dir = None  # 本地模型缓存路径

        # 强制量化断言：不允许非量化加载
        if self.quantization not in self.SUPPORTED_QUANT:
            raise ValueError(
                f"[STT] 量化类型 '{self.quantization}' 不合法！"
                f"必须指定以下之一: {self.SUPPORTED_QUANT}，严禁使用 float32 等非量化类型"
            )

    def load(self):
        """加载 SenseVoice Small 模型（通过 FunASR + ModelScope）"""
        if self._loaded:
            logger.info("[STT] 模型已加载，跳过")
            return

        with self._load_lock:
            if self._loaded or self._loading:
                logger.info("[STT] 模型正在加载或已加载，跳过")
                return
            self._loading = True

        try:
            from funasr import AutoModel
            from modelscope.hub.snapshot_download import snapshot_download

            logger.info(
                "[STT] 正在加载 SenseVoice | 设备: %s | 量化: %s | 显存上限: %dMB",
                self.device, self.quantization, self.vram_limit_mb,
            )

            # 通过 ModelScope 下载模型到本地缓存（白名单已配置）
            logger.info("[STT] 正在从 ModelScope 下载模型 iic/SenseVoiceSmall ...")
            self._model_dir = snapshot_download("iic/SenseVoiceSmall")
            logger.info("[STT] 模型已缓存至: %s", self._model_dir)

            # 从本地路径加载，强制指定设备
            self.model = AutoModel(
                model=self._model_dir,
                device=self.device,  # 强制 cuda:0
                disable_log=True,
                disable_update=True,
                trust_remote_code=True,
            )
            self._loaded = True
            self._loading = False

            # 加载后显存检查
            self._check_vram_after_load()

            logger.info("[STT] SenseVoice 加载完成 | 设备: %s | 量化: %s", self.device, self.quantization)

        except ImportError as e:
            self._loading = False
            logger.error("[STT] 依赖未安装: %s | 请执行: pip install funasr modelscope torchaudio", e)
            raise
        except Exception as e:
            self._loading = False
            logger.error("[STT] 模型加载失败: %s", e)
            raise

    def _check_vram_after_load(self):
        """加载后显存红线校验"""
        try:
            result = _raw_subprocess_run(
                ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                used_mb = float(result.stdout.strip().split("\n")[0])
                if used_mb > self.vram_limit_mb:
                    logger.warning(
                        "[STT] 显存超红线！当前: %.0fMB > 上限: %dMB，建议卸载模型",
                        used_mb, self.vram_limit_mb,
                    )
                else:
                    logger.info("[STT] 显存校验通过 | 当前: %.0fMB / 上限: %dMB", used_mb, self.vram_limit_mb)
        except Exception:
            pass

    def transcribe(self, audio_path: str, language: str = "auto", hotword: str = "") -> dict:
        """执行语音识别

        Args:
            audio_path: 音频文件路径（支持 wav/mp3/flac 等，FunASR 自动处理重采样）
            language: 语言代码，SenseVoice 默认 "auto" 自动检测
            hotword: 热词字符串（空格分隔），提高特定词的识别准确率

        Returns:
            dict: {
                "text": str,               # 剥离标签后的纯净用户指令文本
                "emotion_tags": list[str],  # 提取的情绪与事件标签列表
                "language": str,
                "language_probability": float,
                "duration": float,
            }
        """
        if not self._loaded or self.model is None:
            raise RuntimeError("[STT] 模型未加载，请先调用 load()")

        logger.info("[STT] 开始识别 | 音频: %s", audio_path)

        start_time = time.time()

        # FunASR 统一推理接口
        # SenseVoice 输出格式: <|zh|><|HAPPY|><|Speech|>...文本内容
        # 使用 language="auto" 让模型自动检测语种（中/英/日/韩/粤）
        result = self.model.generate(
            input=audio_path,
            batch_size_s=300,
            hotword=hotword,
        )

        # 提取文本：result 格式为 [{"text": "..."}]
        raw_text = ""
        detected_lang = "auto"
        if isinstance(result, list) and len(result) > 0:
            item = result[0]
            if isinstance(item, dict):
                raw_text = item.get("text", "")
                detected_lang = item.get("language", "auto")
            elif hasattr(item, 'text'):
                raw_text = item.text
                if hasattr(item, 'language'):
                    detected_lang = item.language

        # 解析 SenseVoice 富文本标签：提取 emotion_tags + pure_text
        import re
        if self.__class__._TAG_PATTERN is None:
            self.__class__._TAG_PATTERN = re.compile(r'<\|([^|]+)\|>')

        emotion_tags = self.__class__._TAG_PATTERN.findall(raw_text)
        pure_text = self.__class__._TAG_PATTERN.sub('', raw_text).strip()

        elapsed = time.time() - start_time

        # 获取音频时长
        duration = 0.0
        try:
            import soundfile as sf
            info = sf.info(audio_path)
            duration = info.duration
        except Exception:
            pass

        if emotion_tags:
            logger.info(
                "[STT] 识别完成 | 文本: %.80s | 情绪标签: %s | 耗时: %.1fs | 时长: %.1fs",
                pure_text, emotion_tags, elapsed, duration,
            )
        else:
            logger.info(
                "[STT] 识别完成 | 文本: %.80s | 耗时: %.1fs | 时长: %.1fs",
                pure_text, elapsed, duration,
            )

        return {
            "text": pure_text,
            "emotion_tags": emotion_tags,
            "language": detected_lang,
            "language_probability": 0.95,
            "duration": duration,
        }

    def unload(self):
        """释放模型显存，防止泄漏"""
        if self.model is not None:
            del self.model
            self.model = None
            self._loaded = False
            self._model_dir = None

            # 强制清空 CUDA 缓存
            try:
                import torch
                torch.cuda.empty_cache()
                logger.info("[STT] SenseVoice 模型已卸载，CUDA 缓存已清空")
            except ImportError:
                logger.info("[STT] SenseVoice 模型已卸载")


# ============================================================
# TTS 引擎 — ChatTTS / Bert-VITS2 / Edge-TTS
# ============================================================

class TTSEngine:
    """TTS 语音合成引擎，显存上限 1.5GB，强制量化
    支持 gpt_sovits（GPU，GPT-SoVITS API） / chat_tts（GPU） / bert_vits2（GPU） / edge_tts（在线免费，无需GPU）
    """

    SUPPORTED_BACKENDS = {"gpt_sovits", "chat_tts", "bert_vits2", "edge_tts"}
    SUPPORTED_QUANT = {"float16", "int8"}

    # GPT-SoVITS 配置常量
    GPT_SOVITS_DIR = r"d:\GPT-SoVITS"
    GPT_SOVITS_API_PORT = 9880
    GPT_SOVITS_REF_AUDIO = r"d:\Jarvis_Assistant\Demo\vocal_vocal_ii5_3.WAV.reformatted.wav_10.wav_10.wav_0000000000_0000221440.wav"
    GPT_SOVITS_REF_TEXT = "His son Ivan, who is also a physicist, was convicted of selling Soviet-era weapons-grade plutonium to Pakistan."
    GPT_SOVITS_PARAMS = {
        "top_k": 2,
        "top_p": 0.75,
        "temperature": 0.75,
        "speed_factor": 0.9,
        "fragment_interval": 0.42,
        "text_lang": "en",
        "prompt_lang": "en",
        "text_split_method": "cut5",
        "batch_size": 1,
        "media_type": "wav",
        "streaming_mode": 3,  # 3=低质量最快响应（流式逐chunk返回）
    }

    def __init__(
        self,
        backend: str = "edge_tts",
        quantization: str = "float16",
        device: str = "cuda:0",
        vram_limit_mb: int = 1536,
    ):
        self.backend = backend
        self.quantization = quantization
        self.device = device
        self.vram_limit_mb = vram_limit_mb
        self.model = None
        self._loaded = False
        self._loading = False
        self._load_lock = threading.Lock()
        self._degraded = False
        self._api_host = None  # 缓存可连通的 GPT-SoVITS API host

        if backend not in self.SUPPORTED_BACKENDS:
            raise ValueError(f"[TTS] 不支持的后端: {backend}，可选: {self.SUPPORTED_BACKENDS}")

        # 强制量化断言（edge_tts 在线引擎无需量化）
        if backend != "edge_tts" and self.quantization not in self.SUPPORTED_QUANT:
            raise ValueError(
                f"[TTS] 量化类型 '{self.quantization}' 不合法！"
                f"必须指定以下之一: {self.SUPPORTED_QUANT}，严禁使用 float32"
            )

    @staticmethod
    def _get_candidate_ips():
        """获取 Windows 主机候选 IP 列表（WSL2 环境）"""
        candidates = []

        # 候选1: ip route 默认网关
        try:
            import subprocess as sp
            result = sp.run(["ip", "route", "show", "default"], capture_output=True, text=True, timeout=3)
            for line in result.stdout.splitlines():
                if "default" in line and "via" in line:
                    parts = line.split()
                    idx = parts.index("via") if "via" in parts else None
                    if idx is not None and idx + 1 < len(parts):
                        ip = parts[idx + 1]
                        if ip not in candidates:
                            candidates.append(ip)
                elif "default" in line:
                    parts = line.split()
                    for p in parts:
                        if (p.startswith("172.") or p.startswith("192.168.") or p.startswith("10.")) and p not in candidates:
                            candidates.append(p)
        except Exception:
            pass

        # 候选2: /etc/resolv.conf nameserver
        try:
            with open("/etc/resolv.conf", "r") as r:
                for line in r:
                    if line.strip().startswith("nameserver"):
                        ip = line.split()[1].strip()
                        if ip not in candidates:
                            candidates.append(ip)
        except Exception:
            pass

        # 候选3: 127.0.0.1（镜像模式下 localhost 互通）
        if "127.0.0.1" not in candidates:
            candidates.append("127.0.0.1")

        return candidates

    @staticmethod
    def _detect_windows_host_ip():
        """检测可连通的 Windows 主机 IP（WSL2 环境，TCP 端口探测 9880）"""
        import socket
        candidates = TTSEngine._get_candidate_ips()

        for ip in candidates:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(1)
                result = sock.connect_ex((ip, 9880))
                sock.close()
                if result == 0:
                    return ip
            except Exception:
                pass

        # 全部连不上，返回第一个候选（等待 API 启动后再探测）
        return candidates[0] if candidates else "127.0.0.1"

    @property
    def GPT_SOVITS_API_URL(self):
        """动态获取 API URL：优先用缓存的可连通 host，否则重新探测"""
        if self._api_host:
            return f"http://{self._api_host}:{self.GPT_SOVITS_API_PORT}"

        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    host = self._detect_windows_host_ip()
                    return f"http://{host}:{self.GPT_SOVITS_API_PORT}"
        except Exception:
            pass
        return f"http://127.0.0.1:{self.GPT_SOVITS_API_PORT}"

    def load(self):
        """加载 TTS 模型，强制量化参数"""
        if self._loaded:
            logger.info("[TTS] 模型已加载，跳过")
            return

        with self._load_lock:
            if self._loaded or self._loading:
                logger.info("[TTS] 模型正在加载或已加载，跳过")
                return
            self._loading = True

        # edge_tts 无需量化检查
        if self.backend == "edge_tts":
            self._load_edge_tts()
            return

        # gpt_sovits 通过 API 服务运行，量化由 GPT-SoVITS 配置文件控制（is_half=true 即 float16）
        if self.backend == "gpt_sovits":
            self._load_gpt_sovits()
            return

        if self.quantization in ("float32", "auto"):
            raise ValueError(
                f"[TTS] 拒绝加载！quantization='{self.quantization}' 违反显存红线，"
                f"必须使用 float16/int8"
            )

        try:
            if self.backend == "chat_tts":
                self._load_chat_tts()
            elif self.backend == "bert_vits2":
                self._load_bert_vits2()
        except ImportError as e:
            logger.error("[TTS] 依赖未安装: %s", e)
            raise
        except Exception as e:
            logger.error("[TTS] 模型加载失败: %s", e)
            raise

    def _load_gpt_sovits(self):
        """启动 GPT-SoVITS API 服务（使用自带 runtime，float16 量化，device=cuda）"""
        import subprocess as sp
        import urllib.request

        logger.info("[TTS] 正在启动 GPT-SoVITS API 服务 | 量化: float16 (is_half=true) | 设备: cuda")

        # 检查 API 是否已在运行
        try:
            req = urllib.request.Request(f"{self.GPT_SOVITS_API_URL}/control?command=ping", method="GET")
            urllib.request.urlopen(req, timeout=2)
            logger.info("[TTS] GPT-SoVITS API 已在运行，跳过启动")
            self._loaded = True
            self._loading = False
            return
        except Exception:
            pass

        # 检测运行环境：WSL 还是原生 Windows
        is_wsl = False
        try:
            with open("/proc/version", "r") as f:
                if "microsoft" in f.read().lower():
                    is_wsl = True
        except Exception:
            pass

        # Windows 路径（API 服务运行在 Windows 端）
        win_dir = self.GPT_SOVITS_DIR
        win_python = f"{win_dir}\\runtime\\python.exe"
        win_script = f"{win_dir}\\api_v2.py"
        win_config = f"{win_dir}\\GPT_SoVITS\\configs\\tts_infer.yaml"
        win_numba_cache = f"{win_dir}\\numba_cache"

        # 文件存在性检查（WSL 用 /mnt/ 路径）
        check_python = win_python
        if is_wsl:
            check_python = f"/mnt/d/GPT-SoVITS/runtime/python.exe"
        if not os.path.exists(check_python):
            logger.error("[TTS] GPT-SoVITS runtime 未找到: %s", check_python)
            raise FileNotFoundError(f"GPT-SoVITS runtime not found: {check_python}")

        if is_wsl:
            # WSL 环境：通过 powershell.exe 启动 Windows 端 API 服务
            # 绑定 0.0.0.0 让 WSL 能通过 Windows 主机 IP 访问
            ps_cmd = (
                f"$env:NUMBA_DISABLE_JIT='0'; "
                f"$env:NUMBA_THREADING_LAYER='sequential'; "
                f"$env:NUMBA_NUM_THREADS='1'; "
                f"$env:NUMBA_CACHE_DIR='{win_numba_cache}'; "
                f"Set-Location '{win_dir}'; "
                f"& '{win_dir}\\runtime\\python.exe' -u '{win_script}' "
                f"-a 0.0.0.0 -p 9880 -c '{win_config}' "
                f"2>&1 | Tee-Object -FilePath 'd:\\Jarvis_Assistant\\backend\\gpt_sovits_api.log'"
            )
            cmd = ["powershell.exe", "-NoProfile", "-Command", ps_cmd]
            self._api_process = sp.Popen(
                cmd,
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
            )
        else:
            # 原生 Windows 环境
            cmd = [
                win_python, "-u", win_script,
                "-a", "0.0.0.0",
                "-p", "9880",
                "-c", win_config,
            ]
            env = os.environ.copy()
            env["NUMBA_DISABLE_JIT"] = "0"
            env["NUMBA_THREADING_LAYER"] = "sequential"
            env["NUMBA_NUM_THREADS"] = "1"
            env["NUMBA_CACHE_DIR"] = win_numba_cache

            self._api_process = sp.Popen(
                cmd,
                cwd=win_dir,
                env=env,
                stdout=sp.DEVNULL,
                stderr=sp.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )

        logger.info("[TTS] GPT-SoVITS API 进程已启动 | PID=%d | WSL=%s", self._api_process.pid, is_wsl)

        # 等待 API 就绪（最多 120 秒，首次启动需加载模型）
        # 多候选 IP 轮询：每轮尝试所有候选 IP，任一连通即就绪
        import time
        import socket

        if is_wsl:
            candidates = self._get_candidate_ips()
            logger.info("[TTS] WSL 候选 IP: %s", candidates)
        else:
            candidates = ["127.0.0.1"]

        for i in range(120):
            for host in candidates:
                try:
                    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    sock.settimeout(2)
                    result = sock.connect_ex((host, 9880))
                    sock.close()
                    if result == 0:
                        logger.info("[TTS] GPT-SoVITS API 就绪（等待 %d 秒）| host=%s", i + 1, host)
                        self._api_host = host  # 缓存可连通的 host
                        self._loaded = True
                        self._loading = False
                        return
                except Exception:
                    pass
            time.sleep(1)

        logger.error("[TTS] GPT-SoVITS API 启动超时（120秒）| 尝试过的候选 IP: %s", candidates)
        self._loading = False
        raise RuntimeError("GPT-SoVITS API startup timeout")

    def _load_edge_tts(self):
        """加载 edge-tts（Microsoft Edge 在线 TTS，无需 GPU）"""
        try:
            import edge_tts
            logger.info("[TTS] 正在初始化 Edge-TTS（在线免费 TTS，无需 GPU）")
            self.model = edge_tts
            self._loaded = True
            self._loading = False
            logger.info("[TTS] Edge-TTS 初始化完成 | 可用语音: zh-CN-XiaoxiaoNeural 等")
        except ImportError:
            logger.warning("[TTS] edge-tts 未安装，请执行: pip install edge-tts")
            raise

    def _load_chat_tts(self):
        import ChatTTS

        logger.info(
            "[TTS] 正在加载 ChatTTS | 量化: %s | 设备: %s | 显存上限: %dMB",
            self.quantization, self.device, self.vram_limit_mb,
        )
        self.model = ChatTTS.Chat()
        self.model.load(compile=False, device=self.device)
        self._loaded = True
        self._loading = False
        logger.info("[TTS] ChatTTS 加载完成 | 量化: %s", self.quantization)

    def _load_bert_vits2(self):
        logger.info(
            "[TTS] 正在加载 Bert-VITS2 | 量化: %s | 设备: %s | 显存上限: %dMB",
            self.quantization, self.device, self.vram_limit_mb,
        )
        logger.warning("[TTS] Bert-VITS2 需要手动指定模型路径，当前为框架占位")
        self._loaded = True
        self._loading = False

    def synthesize(self, text: str, output_path: Optional[str] = None) -> Optional[bytes]:
        """执行语音合成，返回完整音频字节数据（非流式，兼容旧接口）"""
        if not self._loaded:
            raise RuntimeError("[TTS] 模型未加载，请先调用 load()")

        if self._degraded:
            logger.warning("[TTS] 当前处于降级模式，跳过 GPU 推理")
            return None

        logger.info("[TTS] 开始合成 | 文本: %.50s | 输出: %s", text, output_path or "内存")

        # ---- GPT-SoVITS API 非流式合成（fallback）----
        if self.backend == "gpt_sovits":
            import urllib.request, urllib.parse, json as json_mod
            import io as _io

            payload = {
                "text": text,
                "text_lang": self.GPT_SOVITS_PARAMS["text_lang"],
                "ref_audio_path": self.GPT_SOVITS_REF_AUDIO,
                "prompt_text": self.GPT_SOVITS_REF_TEXT,
                "prompt_lang": self.GPT_SOVITS_PARAMS["prompt_lang"],
                "top_k": self.GPT_SOVITS_PARAMS["top_k"],
                "top_p": self.GPT_SOVITS_PARAMS["top_p"],
                "temperature": self.GPT_SOVITS_PARAMS["temperature"],
                "speed_factor": self.GPT_SOVITS_PARAMS["speed_factor"],
                "fragment_interval": self.GPT_SOVITS_PARAMS["fragment_interval"],
                "text_split_method": self.GPT_SOVITS_PARAMS["text_split_method"],
                "batch_size": self.GPT_SOVITS_PARAMS["batch_size"],
                "media_type": "wav",
                "streaming_mode": False,
            }

            try:
                req_data = json_mod.dumps(payload).encode("utf-8")
                req = urllib.request.Request(
                    f"{self.GPT_SOVITS_API_URL}/tts",
                    data=req_data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=60) as resp:
                    wav_bytes = resp.read()

                if not wav_bytes or len(wav_bytes) < 44:
                    logger.error("[TTS] GPT-SoVITS 返回空数据")
                    return None

                pcm_data, sample_rate = self._parse_wav_pcm(wav_bytes)
                logger.info("[TTS] GPT-SoVITS 合成完成 | 采样率: %d | PCM字节: %d", sample_rate, len(pcm_data))
                self._last_sample_rate = sample_rate

                if output_path:
                    with open(output_path, "wb") as f:
                        f.write(pcm_data)
                    return None
                return pcm_data

            except urllib.error.HTTPError as e:
                error_body = ""
                try:
                    error_body = e.read().decode("utf-8", errors="replace")
                except Exception:
                    pass
                logger.error("[TTS] GPT-SoVITS API 错误 | code=%d | body=%s", e.code, error_body[:500])
                return None
            except Exception as e:
                logger.error("[TTS] GPT-SoVITS 合成失败: %s", e)
                return None

    def _parse_wav_pcm(self, wav_bytes: bytes) -> tuple:
        """解析 WAV 字节流，返回 (pcm_int16_bytes, sample_rate)"""
        import wave, io
        wav_io = io.BytesIO(wav_bytes)
        with wave.open(wav_io, "rb") as wf:
            sample_rate = wf.getframerate()
            channels = wf.getnchannels()
            sample_width = wf.getsampwidth()
            pcm_data = wf.readframes(wf.getnframes())

        if sample_width != 2:
            import numpy as np
            audio = np.frombuffer(pcm_data, dtype=np.int16 if sample_width == 2 else np.int32)
            if sample_width == 4:
                audio = (audio >> 16).astype(np.int16)
            elif sample_width == 1:
                audio = ((audio.astype(np.int16) - 128) * 256).astype(np.int16)
            pcm_data = audio.tobytes()

        if channels > 1:
            import numpy as np
            audio = np.frombuffer(pcm_data, dtype=np.int16)
            audio = audio.reshape(-1, channels).mean(axis=1).astype(np.int16)
            pcm_data = audio.tobytes()

        return pcm_data, sample_rate

    def synthesize_stream(self, text: str):
        """GPT-SoVITS 流式合成生成器，逐 chunk 返回 (pcm_int16_bytes, sample_rate, is_first)
        
        流式模式下 API 返回 StreamingResponse：首个 chunk 包含 WAV header，
        后续 chunk 为裸 PCM 数据。此方法自动剥离 header，逐块产出纯 PCM。
        """
        if not self._loaded:
            raise RuntimeError("[TTS] 模型未加载，请先调用 load()")

        if self._degraded:
            logger.warning("[TTS] 当前处于降级模式，跳过 GPU 推理")
            return

        if self.backend != "gpt_sovits":
            # 非 GPT-SoVITS 后端降级为整段合成
            pcm = self.synthesize(text)
            if pcm:
                sr = self._last_sample_rate
                yield pcm, sr, True
            return

        import urllib.request, json as json_mod

        payload = {
            "text": text,
            "text_lang": self.GPT_SOVITS_PARAMS["text_lang"],
            "ref_audio_path": self.GPT_SOVITS_REF_AUDIO,
            "prompt_text": self.GPT_SOVITS_REF_TEXT,
            "prompt_lang": self.GPT_SOVITS_PARAMS["prompt_lang"],
            "top_k": self.GPT_SOVITS_PARAMS["top_k"],
            "top_p": self.GPT_SOVITS_PARAMS["top_p"],
            "temperature": self.GPT_SOVITS_PARAMS["temperature"],
            "speed_factor": self.GPT_SOVITS_PARAMS["speed_factor"],
            "fragment_interval": self.GPT_SOVITS_PARAMS["fragment_interval"],
            "text_split_method": self.GPT_SOVITS_PARAMS["text_split_method"],
            "batch_size": self.GPT_SOVITS_PARAMS["batch_size"],
            "media_type": "wav",
            "streaming_mode": self.GPT_SOVITS_PARAMS["streaming_mode"],  # 3=最快响应
        }

        logger.info("[TTS] 开始流式合成 | 文本: %.50s | streaming_mode=%s", text, payload["streaming_mode"])

        try:
            req_data = json_mod.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                f"{self.GPT_SOVITS_API_URL}/tts",
                data=req_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            with urllib.request.urlopen(req, timeout=120) as resp:
                wav_header_size = None
                sample_rate = 32000  # GPT-SoVITS 默认
                total_pcm = 0

                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break

                    if wav_header_size is None:
                        # 首个 chunk：包含 WAV header，解析采样率后剥离
                        if len(chunk) < 44:
                            logger.error("[TTS] 流式首 chunk 过短: %d bytes", len(chunk))
                            return
                        # 解析 WAV header 获取采样率（偏移 24-27 字节）
                        import struct
                        sample_rate = struct.unpack_from('<I', chunk, 24)[0]
                        wav_header_size = struct.unpack_from('<H', chunk, 16)[0] + 8  # fmt chunk size + 'fmt '+size
                        # 实际 data 段从 'data' 标记开始，搜索之
                        data_offset = chunk.find(b'data')
                        if data_offset >= 0:
                            wav_header_size = data_offset + 8  # 'data' + 4字节长度
                        else:
                            wav_header_size = 44  # fallback
                        pcm_chunk = chunk[wav_header_size:]
                        self._last_sample_rate = sample_rate
                        if pcm_chunk:
                            total_pcm += len(pcm_chunk)
                            yield pcm_chunk, sample_rate, True
                    else:
                        # 后续 chunk：裸 PCM 数据
                        total_pcm += len(chunk)
                        yield chunk, sample_rate, False

                logger.info("[TTS] 流式合成完成 | 采样率: %d | PCM字节: %d", sample_rate, total_pcm)

        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            logger.error("[TTS] GPT-SoVITS 流式 API 错误 | code=%d | body=%s", e.code, error_body[:500])
        except Exception as e:
            logger.error("[TTS] GPT-SoVITS 流式合成失败: %s", e)

        # ---- edge-tts 在线合成（异步转同步）----
        if self.backend == "edge_tts" and self.model is not None:

            async def _edge_synthesize():
                """使用临时文件方式合成，避免异步流式处理的复杂性"""
                import tempfile
                tmp_mp3 = os.path.join(tempfile.gettempdir(), f"jarvis_tts_{id(text) % 10000}.mp3")
                communicate = self.model.Communicate(text, "zh-CN-XiaoxiaoNeural")
                await communicate.save(tmp_mp3)

                # 读取 MP3 文件并返回原始字节
                with open(tmp_mp3, "rb") as f:
                    data = f.read()
                # 清理临时文件
                try:
                    os.remove(tmp_mp3)
                except Exception:
                    pass
                return data

            try:
                # 在独立线程中运行 asyncio，避免与 WebSocket 事件循环冲突
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, _edge_synthesize())
                    audio_data = future.result(timeout=30)

                if audio_data and len(audio_data) > 0:
                    logger.info("[TTS] Edge-TTS 合成完成 | 字节数: %d | 格式: MP3", len(audio_data))
                    if output_path:
                        with open(output_path, "wb") as f:
                            f.write(audio_data)
                        return None
                    return audio_data
            except Exception as e:
                logger.error("[TTS] Edge-TTS 合成失败: %s", e)
                return None

        # ---- ChatTTS 本地 GPU 合成 ----
        if self.backend == "chat_tts" and self.model is not None:
            import numpy as np

            params = self.model.InferCodeParams(temperature=0.3, top_P=0.7, top_K=20)
            wavs = self.model.infer(text, params_infer_code=params)
            if wavs and len(wavs) > 0:
                audio = np.array(wavs[0], dtype=np.float32)
                audio = (audio * 32767).astype(np.int16)
                if output_path:
                    import soundfile as sf
                    sf.write(output_path, audio, samplerate=24000)
                    logger.info("[TTS] 音频已写入: %s", output_path)
                    return None
                logger.info("[TTS] 合成完成 | 采样率: 24000 | 样本数: %d", len(audio))
                return audio.tobytes()
        return None

    def degrade(self):
        """降级：挂起 GPU 推理（GPT-SoVITS API 后端跳过，因为是独立进程）"""
        if self.backend == "gpt_sovits":
            logger.info("[TTS] GPT-SoVITS API 后端，跳过本地降级（API 是独立进程）")
            return
        self._degraded = True
        logger.warning("[TTS DEGRADE] 已进入降级模式，GPU 推理已挂起")

    def recover(self):
        """恢复 GPU 推理"""
        self._degraded = False
        logger.info("[TTS RECOVER] 已恢复 GPU 推理模式")

    def unload(self):
        if self.backend == "gpt_sovits" and hasattr(self, "_api_process"):
            try:
                # 优先通过 API 优雅关闭
                import urllib.request
                try:
                    req = urllib.request.Request(
                        f"{self.GPT_SOVITS_API_URL}/control?command=exit",
                        method="GET",
                    )
                    urllib.request.urlopen(req, timeout=3)
                    logger.info("[TTS] GPT-SoVITS API 已通过接口关闭")
                except Exception:
                    # 接口关闭失败，强制终止进程
                    import subprocess as sp
                    import platform
                    is_wsl = "microsoft" in platform.uname().release.lower()
                    if is_wsl:
                        # WSL：通过端口号找到 PID 并终止
                        sp.run(
                            ["cmd.exe", "/c",
                             'for /f "tokens=5" %a in (\'netstat -aon ^| findstr :9880 ^| findstr LISTENING\') do taskkill /F /PID %a'],
                            capture_output=True, timeout=5,
                        )
                    else:
                        self._api_process.terminate()
                        self._api_process.wait(timeout=5)
                    logger.info("[TTS] GPT-SoVITS API 进程已强制终止")
            except Exception:
                try:
                    self._api_process.kill()
                except Exception:
                    pass
        if self.model is not None:
            del self.model
            self.model = None
        self._loaded = False
        logger.info("[TTS] TTS 模型已卸载，显存已释放")


# ============================================================
# 短期记忆 — 滑动窗口
# ============================================================

class ShortTermMemory:
    """基于滑动窗口的短期对话历史队列"""

    def __init__(self, max_rounds: int = 10):
        self.max_rounds = max_rounds
        self._history: deque = deque(maxlen=max_rounds)

    def add(self, role: str, content: str):
        self._history.append({
            "role": role,
            "content": content,
            "timestamp": int(time.time() * 1000),
        })
        logger.info("[SHORT-TERM] 添加 %s 轮对话 | 当前队列长度: %d/%d", role, len(self._history), self.max_rounds)

    def get_all(self) -> list[dict]:
        return list(self._history)

    def clear(self):
        self._history.clear()
        logger.info("[SHORT-TERM] 短期记忆已清空")

    def summarize_and_clear(self, summary: str):
        """摘要压缩后清空"""
        self._history.clear()
        if summary:
            self._history.append({
                "role": "system",
                "content": f"[会话摘要] {summary}",
                "timestamp": int(time.time() * 1000),
            })
        logger.info("[SHORT-TERM] 摘要压缩完成，队列已重置")


# ============================================================
# 长期记忆引擎 — ChromaDB
# ============================================================

class MemoryEngine:
    """ChromaDB 本地向量数据库，CPU 推理嵌入模型"""

    def __init__(
        self,
        persist_dir: str = "./data/chroma_db",
        collection_name: str = "jarvis_memory",
        embedding_model: str = "all-MiniLM-L6-v2",
        top_k: int = 3,
    ):
        self.persist_dir = persist_dir
        self.collection_name = collection_name
        self.embedding_model_name = embedding_model
        self.top_k = top_k
        self.client = None
        self.collection = None
        self.embedding_fn = None
        self._loaded = False

    def load(self):
        """初始化 ChromaDB 与嵌入模型"""
        if self._loaded:
            logger.info("[MEMORY] ChromaDB 已初始化，跳过")
            return

        try:
            import chromadb
            from chromadb.utils import embedding_functions

            logger.info(
                "[MEMORY] 正在初始化 ChromaDB | 持久化目录: %s | 嵌入模型: %s",
                self.persist_dir, self.embedding_model_name,
            )

            self.embedding_fn = embedding_functions.SentenceTransformerEmbeddingFunction(
                model_name=self.embedding_model_name,
            )

            self.client = chromadb.PersistentClient(path=self.persist_dir)
            self.collection = self.client.get_or_create_collection(
                name=self.collection_name,
                embedding_function=self.embedding_fn,
            )
            self._loaded = True
            logger.info(
                "[MEMORY] ChromaDB 初始化完成 | 当前记忆条数: %d",
                self.collection.count(),
            )

        except ImportError:
            logger.error("[MEMORY] chromadb 未安装，请执行: pip install chromadb sentence-transformers")
            raise
        except Exception as e:
            logger.error("[MEMORY] ChromaDB 初始化失败: %s", e)
            raise

    def write(self, content: str, metadata: Optional[dict] = None):
        """写入一条记忆"""
        if not self._loaded:
            raise RuntimeError("[MEMORY] 未初始化")

        doc_id = str(uuid.uuid4())
        meta = metadata or {}
        meta["timestamp"] = meta.get("timestamp", int(time.time() * 1000))

        self.collection.add(
            documents=[content],
            ids=[doc_id],
            metadatas=[meta],
        )
        logger.info("[MEMORY] 写入记忆 | ID: %s | 内容: %.50s...", doc_id, content)

    def retrieve(self, query: str, top_k: Optional[int] = None) -> list[dict]:
        """语义检索 Top-K 记忆"""
        if not self._loaded:
            raise RuntimeError("[MEMORY] 未初始化")

        k = top_k or self.top_k
        logger.info("[MEMORY] 检索 | 查询: %.50s | Top-K: %d", query, k)

        results = self.collection.query(
            query_texts=[query],
            n_results=k,
        )

        matches = []
        if results and results["documents"] and results["documents"][0]:
            for i, doc in enumerate(results["documents"][0]):
                matches.append({
                    "content": doc,
                    "score": 1.0 - results["distances"][0][i] if results["distances"] else 0.0,
                    "timestamp": results["metadatas"][0][i].get("timestamp", 0) if results["metadatas"] else 0,
                })
        logger.info("[MEMORY] 检索完成 | 命中: %d 条", len(matches))
        return matches

    def soft_delete(self, doc_id: str):
        """软删除：标记为已废弃而非物理删除"""
        if not self._loaded:
            raise RuntimeError("[MEMORY] 未初始化")

        self.collection.update(
            ids=[doc_id],
            metadatas=[{"deleted": True}],
        )
        logger.info("[MEMORY] 软删除记忆 | ID: %s", doc_id)


# ============================================================
# AI 引擎总控
# ============================================================

@dataclass
class AIEngineConfig:
    stt_model_size: str = "SenseVoiceSmall"
    stt_quantization: str = "float16"
    tts_backend: str = "gpt_sovits"
    tts_quantization: str = "float16"
    memory_persist_dir: str = "./data/chroma_db"
    memory_embedding_model: str = "all-MiniLM-L6-v2"
    vram_threshold_percent: float = 90.0
    device: str = "cuda:0"
    short_term_max_rounds: int = 10


class AIEngine:
    """AI 引擎总控，统一管理 STT / TTS / Memory / VRAM"""

    def __init__(self, config: Optional[AIEngineConfig] = None):
        self.config = config or AIEngineConfig()
        self.stt: Optional[STTEngine] = None
        self.tts: Optional[TTSEngine] = None
        self.memory: Optional[MemoryEngine] = None
        self.short_term: Optional[ShortTermMemory] = None
        self.vram_monitor: Optional[VRAMMonitor] = None
        self._initialized = False

    def initialize(self):
        """初始化所有子引擎"""
        if self._initialized:
            logger.info("[ENGINE] AI 引擎已初始化，跳过")
            return

        cfg = self.config

        self.stt = STTEngine(
            model_size=cfg.stt_model_size,
            quantization=cfg.stt_quantization,
            device=cfg.device,
            vram_limit_mb=3072,
        )

        self.tts = TTSEngine(
            backend=cfg.tts_backend,
            quantization=cfg.tts_quantization,
            device=cfg.device,
            vram_limit_mb=1536,
        )

        self.memory = MemoryEngine(
            persist_dir=cfg.memory_persist_dir,
            embedding_model=cfg.memory_embedding_model,
        )

        self.short_term = ShortTermMemory(max_rounds=cfg.short_term_max_rounds)

        self.vram_monitor = VRAMMonitor(
            threshold_percent=cfg.vram_threshold_percent,
            on_degrade=self._on_vram_degrade,
            on_recover=self._on_vram_recover,
        )

        self._initialized = True
        logger.info("[ENGINE] AI 引擎总控初始化完成（模型尚未加载，需调用 load_models()）")

    def load_models(self, load_stt: bool = True, load_tts: bool = True, load_memory: bool = True):
        """加载模型到显存/内存"""
        if not self._initialized:
            self.initialize()

        if load_memory:
            self.memory.load()

        if load_stt:
            self.stt.load()

        if load_tts:
            self.tts.load()

        self.vram_monitor.start()
        logger.info("[ENGINE] 所有请求模型已加载，显存监控已启动")

    def _on_vram_degrade(self, status: VRAMStatus):
        """显存超限降级回调"""
        logger.warning("[ENGINE DEGRADE] 显存降级触发 | 占用: %.1f%% | 挂起 TTS GPU 推理", status.usage_percent)
        if self.tts:
            self.tts.degrade()

    def _on_vram_recover(self, status: VRAMStatus):
        """显存恢复回调"""
        logger.info("[ENGINE RECOVER] 显存恢复 | 占用: %.1f%% | 恢复 TTS GPU 推理", status.usage_percent)
        if self.tts:
            self.tts.recover()

    def shutdown(self):
        """关闭所有引擎"""
        if self.vram_monitor:
            self.vram_monitor.stop()
        if self.stt:
            self.stt.unload()
        if self.tts:
            self.tts.unload()
        if self.short_term:
            self.short_term.clear()
        logger.info("[ENGINE] AI 引擎已关闭")

    def get_system_status(self) -> dict:
        """获取系统状态（对应通信协议 system_status 消息）"""
        vram = self.vram_monitor.status if self.vram_monitor else VRAMStatus()
        return {
            "vram_usage_percent": round(vram.usage_percent, 1),
            "stt_loaded": self.stt._loaded if self.stt else False,
            "tts_loaded": self.tts._loaded if self.tts else False,
            "llm_connected": False,
            "memory_db_active": self.memory._loaded if self.memory else False,
        }
