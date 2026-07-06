"""
Jarvis Assistant — 后端主入口
整合 SenseVoice Small (STT) / Edge-TTS / ChromaDB / WebSocket 服务端
WebSocket 绑定 0.0.0.0 允许跨环境网络穿透
全双工流水线：音频接收 → STT → LLM → TTS → 音频回传 + Volume RMS
显存守护线程 + OOM 测试后门
"""

import os
import sys
import re
import json
import time
import base64
import struct
import asyncio
import datetime
import logging
import threading
import traceback
from pathlib import Path
from typing import Optional

# 自动加载 .env 文件（如果存在）
_dotenv = Path(__file__).parent / ".env"
if _dotenv.exists():
    for line in _dotenv.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        if k.strip() not in os.environ:
            os.environ[k.strip()] = v.strip().strip("\"'")

import websockets
from websockets.server import serve

# 确保当前目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import security_interceptor  # noqa: F401 — import 即激活安全拦截
from ai_engine import AIEngine, AIEngineConfig, VRAMMonitor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")

# ============================================================
# 动态应用网址映射表（app_launch 失败时自动降级为 open_url）
# ============================================================

APP_WEB_URLS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app_web_urls.json")
_app_web_urls: dict[str, str] = {}
_action_result_waiters: dict[str, asyncio.Future] = {}


def _load_app_web_urls():
    """Load app-to-URL mapping from JSON file."""
    global _app_web_urls
    try:
        if os.path.exists(APP_WEB_URLS_PATH):
            with open(APP_WEB_URLS_PATH, "r", encoding="utf-8") as f:
                _app_web_urls = json.load(f)
            logger.info("[APP_URLS] Loaded %d app URL mappings", len(_app_web_urls))
    except Exception as e:
        logger.warning("[APP_URLS] Load failed: %s", e)
        _app_web_urls = {}


def _save_app_web_url(app_name: str, url: str):
    """Save a single app-to-URL mapping to JSON file."""
    global _app_web_urls
    _app_web_urls[app_name] = url
    try:
        with open(APP_WEB_URLS_PATH, "w", encoding="utf-8") as f:
            json.dump(_app_web_urls, f, ensure_ascii=False, indent=2)
        logger.info("[APP_URLS] Cached | app=%s | url=%s", app_name, url)
    except Exception as e:
        logger.warning("[APP_URLS] Save failed: %s", e)


def _get_app_web_url(app_name: str) -> str:
    """Look up cached URL for an app name."""
    return _app_web_urls.get(app_name, "")


# 启动时加载映射表
_load_app_web_urls()

# ============================================================
# 全局配置
# ============================================================

WS_HOST = os.environ.get("JARVIS_WS_HOST", "0.0.0.0")
WS_PORT = int(os.environ.get("JARVIS_WS_PORT", "8765"))
LLM_API_KEY = os.environ.get("JARVIS_LLM_API_KEY", "")
LLM_API_BASE = os.environ.get("JARVIS_LLM_API_BASE", "https://open.bigmodel.cn/api/paas/v4")
LLM_MODEL = os.environ.get("JARVIS_LLM_MODEL", "glm-4")

# OOM 测试后门开关
OOM_TEST_MODE = False

# ============================================================
# AI 引擎实例（全局单例）
# ============================================================

engine: Optional[AIEngine] = None


def init_engine():
    global engine
    config = AIEngineConfig()
    engine = AIEngine(config)
    engine.initialize()

    # 仅初始化 ChromaDB（轻量），STT/TTS 按需懒加载
    engine.memory.load()
    engine.vram_monitor.start()

    # 注册 Function Calling 执行器
    register_all_executors()

    logger.info("[MAIN] AI 引擎初始化完成 | ChromaDB 已加载 | 显存监控已启动 | FC 执行器已注册")

    # TTS 预加载（后台线程，不阻塞 WebSocket 启动）
    import threading
    def _preload_tts():
        try:
            logger.info("[TTS] 后台预加载已启动（跟随启动项）")
            engine.tts.load()
            logger.info("[TTS] 后台预加载完成")
        except Exception as e:
            logger.error("[TTS] 后台预加载失败: %s", e)

    tts_thread = threading.Thread(target=_preload_tts, name="tts-preload", daemon=True)
    tts_thread.start()


def ensure_stt_loaded():
    if engine and engine.stt and not engine.stt._loaded:
        engine.stt.load()


def ensure_tts_loaded():
    if engine and engine.tts and not engine.tts._loaded:
        engine.tts.load()


# ============================================================
# Volume RMS 计算与推送
# ============================================================

def compute_rms(pcm_bytes: bytes) -> float:
    """计算 PCM int16 音频的 RMS，归一化至 [0, 1]"""
    if len(pcm_bytes) < 2:
        return 0.0
    count = len(pcm_bytes) // 2
    total = 0.0
    for i in range(count):
        sample = struct.unpack_from("<h", pcm_bytes, i * 2)[0]
        total += sample * sample
    rms = (total / count) ** 0.5
    return min(rms / 32767.0, 1.0)


def estimate_frequency(pcm_bytes: bytes, sample_rate: int = 24000) -> float:
    """简单过零率频率估计"""
    if len(pcm_bytes) < 4:
        return 0.0
    samples = []
    for i in range(0, len(pcm_bytes) - 1, 2):
        samples.append(struct.unpack_from("<h", pcm_bytes, i)[0])
    if len(samples) < 2:
        return 0.0
    crossings = 0
    for i in range(1, len(samples)):
        if (samples[i - 1] >= 0 and samples[i] < 0) or (samples[i - 1] < 0 and samples[i] >= 0):
            crossings += 1
    duration = len(samples) / sample_rate
    return (crossings / 2.0) / duration if duration > 0 else 0.0


# ============================================================
# LLM 调用（云端 API）
# ============================================================

async def call_llm(text: str, memories: list[dict], emotion_tags: list[str] = None, extra_context: str = "", no_tools: bool = False) -> dict:
    """调用云端 LLM，返回回复文本或 Function Calling 指令

    Args:
        text: 用户输入的纯净文本
        memories: 记忆检索结果
        emotion_tags: SenseVoice 提取的情绪/事件标签列表
        extra_context: 额外上下文（如搜索结果），用于二次调用 LLM 时传入
    """
    if not LLM_API_KEY:
        return {"type": "text", "content": f"[模拟回复] 收到：{text}"}

    try:
        import httpx

        memory_ctx = ""
        if memories:
            memory_ctx = "\n".join(f"- {m['content']}" for m in memories[:3])
            memory_ctx = f"\n\n[用户历史记忆]\n{memory_ctx}\n"

        # 情绪标签注入：将 SenseVoice 提取的情绪/环境标签动态注入 System Prompt
        emotion_ctx = ""
        if emotion_tags:
            # 过滤掉语种标签（zh/en/ja等）和 ITN 标签，只保留情绪与事件标签
            _skip_tags = {"zh", "en", "ja", "ko", "yue", "woITN", "wITN", "Speech", "Music", "Noise"}
            filtered_tags = [t for t in emotion_tags if t not in _skip_tags]
            if filtered_tags:
                tag_list = ", ".join(f"<|{t}|>" for t in filtered_tags)
                emotion_ctx = (
                    f"\n\n[用户情绪/环境感知]\n"
                    f"检测到用户当前的情感/环境标签为：[{tag_list}]。"
                    f"请在回答时匹配用户的情绪状态（例如用户愤怒时请简短安抚，开心时可以幽默回应，"
                    f"咳嗽时可以关心健康，大笑时可以轻松互动）。"
                )

        system_prompt = (
            "你是 Jarvis，一个 PC 桌面端智能语音助手。\n\n"
            "## 核心规则（必须严格遵守）\n"
            "1. 当用户的请求涉及以下操作时，你必须调用对应的 Function Calling 工具，禁止用文字描述操作：\n"
            "   - 音量/多媒体/窗口/锁屏 → 调用 device_control\n"
            "   - 打开/启动应用 → 必须调用 app_launch（严禁直接调用 open_url）\n"
            "   - 无论应用是否有桌面版，只要用户说'打开XX'，必须先调用 app_launch\n"
            "   - 仅当 app_launch 失败后，系统会自动降级为 open_url 打开网页版\n"
            "   - 禁止自行判断应用是否安装，直接调用 app_launch 让系统全盘搜索\n"
            "   - 关闭/退出应用 → 调用 app_close\n"
            "   - 询问事实性信息（比赛结果/天气/新闻/人物等）→ 调用 web_query（直接回答，不打开浏览器）\n"
            "   - 注意：'今日/今天/最新XX战况/赛况/赛果/比分/结果'是询问最新赛事信息，必须调用 news_query（不是 web_query，也不是 get_current_time）\n"
            "   - 注意：'今日天气'/'最新新闻'/'突发新闻'等需要最新资讯的场景，必须调用 news_query\n"
            "   - web_query 用于一般性知识搜索（如'量子力学是什么'、'北京人口多少'），news_query 用于需要最新信息的场景\n"
            "   - 搜索网页/用浏览器搜索 → 调用 web_search\n"
            "   - 打开网站/访问网页 → 调用 open_url\n"
            "   - 搜索文件 → 调用 file_search_everything\n"
            "   - 读取文件 → 调用 file_read_content\n"
            "   - 仅当用户直接问'现在几点'/'今天几号'时才调用 get_current_time\n"
            "   - 查询天气 → 调用 weather_query（传入城市名）\n"
            "   - 查询科技新闻/AI资讯 → 调用 hackernews_query\n"
            "2. 绝对不要用文字模仿工具调用结果（如'已执行操作: app_close'），必须实际调用对应工具。\n"
            "3. 你不需要知道应用是否安装在哪里，只要用户说'打开XX'，就直接调用 app_launch，系统会自动全盘搜索。禁止说'我找不到'或'没有这个应用'。\n"
            "4. 回答尽量控制在 1-2 句话以内，不要拓展，不要列选项。\n"
            "5. 必须用英文回答，无论用户使用什么语言输入。语气简洁自然，像JARVIS一样专业。\n"
            "6. 无法完成时只说一句原因，不要道歉或建议替代方案。\n"
            f"7. 当前日期：{datetime.datetime.now().strftime('%Y年%m月%d日')} {['星期一','星期二','星期三','星期四','星期五','星期六','星期日'][datetime.datetime.now().weekday()]}。\n"
            "8. 调用 web_query/news_query 时，query 只写 2-4 个核心关键词，禁止添加年份、日期、月份（如'2026年6月21日'）。例如用户问'今日世界杯赛况'，query 应为'世界杯赛况'而非'世界杯 2026年6月21日 赛况'。"
            + emotion_ctx
        )

        if extra_context:
            system_prompt += (
                "\n\n[在线搜索结果参考]\n"
                f"{extra_context}\n"
                "请严格根据以上搜索结果回答用户问题。规则：\n"
                "1. 只使用搜索结果中明确出现的信息，绝对不要编造或推测。\n"
                "2. 如果搜索结果与用户问题无关或未包含答案，直接说'抱歉，未找到相关信息'。\n"
                "3. 禁止说'正在查询'、'请稍等'、'我可以帮你查'、'可前往XXX网站查看'等推脱词。\n"
                "4. 如果搜索结果只有网站介绍没有具体数据，直接说'抱歉，未找到具体的比赛结果'。\n"
                "5. 禁止使用emoji表情符号，通过语气词（如'呢'、'呀'、'哦'）表达感情。\n"
        "6. 禁止输出JSON格式或模拟工具调用，只返回纯文本回答。\n"
        "7. 禁止输出内心独白或思考过程（如'用户没有提出新问题'、'我无法理解'等），直接回答用户。\n"
            )

        # 动态注入已知无桌面版应用网址
        if _app_web_urls:
            url_list = "\n".join(f"   - {k} → {v}" for k, v in _app_web_urls.items())
            system_prompt += f"\n\n[已知无桌面版应用]\n以下应用请直接用 open_url 打开网页版：\n{url_list}"

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "device_control",
                    "description": "控制设备环境：音量、多媒体、窗口、锁屏",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": [
                                "volume_up", "volume_down", "volume_mute", "volume_set", "volume_get",
                                "media_play_pause", "media_next", "media_prev",
                                "window_minimize", "window_maximize", "system_lock",
                            ]},
                            "step": {"type": "integer", "description": "音量调节步长（volume_up/down 使用）"},
                            "level": {"type": "integer", "description": "音量设置值 0-100（volume_set 使用）"},
                        },
                        "required": ["action"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "app_launch",
                    "description": "启动/打开指定的桌面应用程序。常见应用：微信、QQ、抖音、哔哩哔哩/B站、网易云音乐、Steam、VSCode、记事本、计算器、浏览器、Word、Excel、PPT、画图、终端、斗鱼直播",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string", "description": "应用名称"},
                        },
                        "required": ["app_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "app_close",
                    "description": "关闭/退出指定的应用程序",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "app_name": {"type": "string", "description": "要关闭的应用名称"},
                        },
                        "required": ["app_name"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "用浏览器搜索指定内容，会打开默认浏览器并搜索关键词。仅当用户明确要求'打开浏览器搜索'时使用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "要搜索的关键词或内容"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "news_query",
                    "description": "查询实时新闻和最新资讯（如今日比赛结果、最新战况、突发新闻、今日天气等）。优先于web_query用于需要最新信息的场景。query必须是2-4个核心关键词。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "2-4个核心搜索关键词，如'世界杯赛果'、'NBA今日比分'、'北京天气'"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "web_query",
                    "description": "在线搜索并直接返回搜索结果摘要，用于回答用户的事实性问题（如比赛结果、天气、新闻、人物信息等）。使用此工具后系统会根据搜索结果直接回答，不会打开浏览器。query必须是2-4个核心关键词，不要写完整句子，不要包含日期年份。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "2-4个核心搜索关键词，如'世界杯赛果'、'NBA比分'、'北京天气'，不要写完整句子"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "open_url",
                    "description": "直接用浏览器打开指定网址。当用户要求打开某个网站/网页时使用此工具而非 web_search。常见网址：QQ邮箱=mail.qq.com, 网易邮箱=mail.163.com, GitHub=github.com, B站=bilibili.com, 知乎=zhihu.com, 微博=weibo.com, 淘宝=taobao.com, 京东=jd.com, 百度=baidu.com, 小红书=xiaohongshu.com, 抖音=douyin.com, 网易云音乐=music.163.com, QQ音乐=y.qq.com, 斗鱼直播=douyu.com, 虎牙直播=huya.com",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "url": {"type": "string", "description": "要打开的完整网址，如 https://mail.qq.com"},
                            "description": {"type": "string", "description": "网址的中文描述，如'QQ邮箱'"},
                        },
                        "required": ["url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "file_search_everything",
                    "description": "使用 Everything 搜索文件",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "搜索关键词"},
                        },
                        "required": ["query"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "file_read_content",
                    "description": "读取文件内容（仅限 .txt .md .pdf .docx .csv）",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {"type": "string", "description": "文件绝对路径"},
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "get_current_time",
                    "description": "获取当前日期和时间",
                    "parameters": {
                        "type": "object",
                        "properties": {},
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "weather_query",
                    "description": "查询指定城市的天气情况（当前温度、湿度、风速、天气状况及未来3天预报）。当用户问'今天天气怎么样'/'明天会下雨吗'/'北京气温多少'时调用此工具。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "city": {"type": "string", "description": "城市名称，如'北京'、'上海'、'深圳'"},
                        },
                        "required": ["city"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "hackernews_query",
                    "description": "查询Hacker News科技新闻热榜（英文科技资讯、AI、编程、创业等）。当用户问'有什么科技新闻'/'最新AI资讯'/'Hacker News热榜'时调用。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "可选的搜索关键词，为空则返回热榜"},
                        },
                    },
                },
            },
        ]

        messages = [{"role": "system", "content": system_prompt + memory_ctx}]
        if engine and engine.short_term:
            for h in engine.short_term.get_all():
                # 过滤掉旧的矛盾回复（如"无法关闭应用"等与当前工具集冲突的内容）
                content = h["content"]
                if any(kw in content for kw in ["无法直接关闭", "无法关闭应用", "找不到", "没有找到", "没有这个应用", "暂时无法帮你打开", "无法帮你打开", "[工具已调用]", "[工具调用失败]"]):
                    continue
                messages.append({"role": h["role"], "content": content})
        messages.append({"role": "user", "content": text})

        async with httpx.AsyncClient(timeout=30) as client:
            request_body = {
                "model": LLM_MODEL,
                "messages": messages,
            }
            if not no_tools:
                request_body["tools"] = tools
                request_body["tool_choice"] = "auto"
            resp = await client.post(
                f"{LLM_API_BASE}/chat/completions",
                headers={"Authorization": f"Bearer {LLM_API_KEY}"},
                json=request_body,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]
        msg = choice["message"]

        if msg.get("tool_calls"):
            tc = msg["tool_calls"][0]
            func_name = tc["function"]["name"]
            func_args = json.loads(tc["function"]["arguments"])
            return {"type": "function_call", "name": func_name, "args": func_args, "id": tc["id"]}

        return {"type": "text", "content": msg.get("content", "")}

    except Exception as e:
        logger.error("[LLM] 调用失败: %s", e)
        return {"type": "text", "content": f"[LLM 调用异常] {e}"}


# ============================================================
# Function Calling 执行器
# ============================================================

from security_interceptor import validate_action

ACTION_EXECUTORS = {}


def register_all_executors():
    """注册所有 Function Calling 执行器"""

    # ---- device_control：通过 WebSocket 下发给前端（Tauri/Windows）执行 ----

    def _device_control(args: dict) -> dict:
        """设备控制操作，需要路由到前端 Windows 宿主机执行"""
        return {"__route": "frontend", "category": "device_control", "params": args}

    def _app_launch(args: dict) -> dict:
        """应用启动，路由到前端执行"""
        return {"__route": "frontend", "category": "app_management", "params": args}

    def _app_close(args: dict) -> dict:
        """应用关闭，路由到前端执行"""
        return {"__route": "frontend", "category": "app_management", "params": args}

    def _web_search(args: dict) -> dict:
        """网页搜索，路由到前端执行"""
        return {"__route": "frontend", "category": "web_search", "params": args}

    def _open_url(args: dict) -> dict:
        """打开网址，路由到前端执行"""
        return {"__route": "frontend", "category": "web_search", "params": args}

    def _web_query(args: dict) -> dict:
        """在线搜索并返回结果摘要（后端本地执行，不打开浏览器）"""
        import urllib.request
        import urllib.parse
        import re as re_mod

        query = args.get("query", "")
        if not query:
            return {"success": False, "error": "查询内容为空"}

        # 清理查询中的年份和日期，避免 Bing 搜索偏题（如"2026年世界杯"→"世界杯"）
        clean_query = re_mod.sub(r'\d{4}年?', '', query)
        clean_query = re_mod.sub(r'\d{1,2}月\d{1,2}日?', '', clean_query)
        clean_query = re_mod.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            clean_query = query

        # 限制查询长度：Bing 对短关键词效果更好，截取前 3 个词
        words = clean_query.split()
        if len(words) > 4:
            clean_query = ' '.join(words[:4])

        url = f"https://www.bing.com/search?q={urllib.parse.quote(clean_query)}&count=10"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }

        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                html = resp.read().decode("utf-8", errors="replace")

            results = []

            # 方案1：提取 b_caption 中的摘要（Bing 搜索结果摘要容器）
            captions = re_mod.findall(r'<div class="b_caption"[^>]*>(.*?)</div>', html, re_mod.DOTALL)
            for cap in captions:
                # 提取摘要 <p> 标签
                snippet_m = re_mod.search(r'<p[^>]*>(.*?)</p>', cap, re_mod.DOTALL)
                if snippet_m:
                    clean = re_mod.sub(r'<[^>]+>', '', snippet_m.group(1)).strip()
                    clean = re_mod.sub(r'&[a-z]+;', ' ', clean)
                    clean = re_mod.sub(r'&#\d+;', ' ', clean)
                    clean = re_mod.sub(r'\s+', ' ', clean).strip()
                    if len(clean) > 20:
                        results.append(clean)
                if len(results) >= 5:
                    break

            # 方案2：如果方案1没结果，尝试提取 b_algo 块中的所有文本
            if not results:
                algo_blocks = re_mod.findall(r'<li class="b_algo"[^>]*>(.*?)</li>', html, re_mod.DOTALL)
                for block in algo_blocks:
                    # 提取所有 <p> 标签
                    snippets = re_mod.findall(r'<p[^>]*>(.*?)</p>', block, re_mod.DOTALL)
                    for s in snippets:
                        clean = re_mod.sub(r'<[^>]+>', '', s).strip()
                        clean = re_mod.sub(r'&[a-z]+;', ' ', clean)
                        clean = re_mod.sub(r'&#\d+;', ' ', clean)
                        clean = re_mod.sub(r'\s+', ' ', clean).strip()
                        if len(clean) > 20:
                            results.append(clean)
                    if len(results) >= 5:
                        break

            if not results:
                return {"success": False, "error": "未找到搜索结果"}

            summary = "\n".join(f"{i+1}. {r}" for i, r in enumerate(results[:5]))
            logger.info("[WEB_QUERY] 搜索完成 | query=%s | 结果数=%d", clean_query, len(results[:5]))
            return {"success": True, "query": clean_query, "summary": summary}
        except Exception as e:
            logger.error("[WEB_QUERY] 搜索失败: %s", e)
            return {"success": False, "error": str(e)}

    # ---- news_query：搜狗新闻搜索 ----

    def _news_query(args: dict) -> dict:
        """搜狗新闻搜索，获取实时新闻和最新资讯"""
        import urllib.request
        import urllib.parse

        query = args.get("query", "")
        if not query:
            return {"success": False, "error": "查询内容为空"}

        # 清理查询
        clean_query = re.sub(r'\d{4}年?', '', query)
        clean_query = re.sub(r'\d{1,2}月\d{1,2}日?', '', clean_query)
        clean_query = re.sub(r'\s+', ' ', clean_query).strip()
        if not clean_query:
            clean_query = query
        words = clean_query.split()
        if len(words) > 4:
            clean_query = ' '.join(words[:4])

        url = f"https://news.sogou.com/news?query={urllib.parse.quote(clean_query)}&sort=1"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
                # 搜狗新闻可能返回 UTF-8 或 GBK
                charset = resp.headers.get_content_charset() or 'utf-8'
                html = raw.decode(charset, errors='replace')

            results = []

            # 搜狗新闻结构：class="vrwrap" 块
            vrwrap_blocks = re.findall(r'class="vrwrap"[^>]*>(.*?)(?=class="vrwrap"|class="footer")', html, re.DOTALL)
            for block in vrwrap_blocks:
                # 提取标题：h3.vr-title > a
                title_m = re.search(r'<h3[^>]*class="vr-title[^"]*"[^>]*>.*?<a[^>]*>(.*?)</a>', block, re.DOTALL)
                title = re.sub(r'<[^>]+>', '', title_m.group(1)).strip() if title_m else ""
                # 提取摘要：p.star-wiki
                snippet_m = re.search(r'<p[^>]*class="star-wiki"[^>]*>(.*?)</p>', block, re.DOTALL)
                snippet = re.sub(r'<[^>]+>', '', snippet_m.group(1)).strip() if snippet_m else ""
                # 提取来源/时间：p.news-from > span
                source_m = re.search(r'<p[^>]*class="news-from[^"]*"[^>]*>(.*?)</p>', block, re.DOTALL)
                source = re.sub(r'<[^>]+>', '', source_m.group(1)).strip() if source_m else ""

                if title:
                    entry_parts = []
                    if source:
                        entry_parts.append(source)
                    entry_parts.append(title)
                    if snippet:
                        entry_parts.append(snippet[:150])
                    entry = ' | '.join(entry_parts)
                    entry = re.sub(r'&[a-z]+;', ' ', entry)
                    entry = re.sub(r'&#\d+;', ' ', entry)
                    entry = re.sub(r'\s+', ' ', entry).strip()
                    if len(entry) > 10:
                        results.append(entry)
                if len(results) >= 5:
                    break

            # 降级：如果 vrwrap 无结果，尝试 h3 > a 直接提取
            if not results:
                all_h3 = re.findall(r'<h3[^>]*>\s*<a[^>]*>(.*?)</a>\s*</h3>', html, re.DOTALL)
                for h in all_h3:
                    clean = re.sub(r'<[^>]+>', '', h).strip()
                    clean = re.sub(r'&[a-z]+;', ' ', clean)
                    clean = re.sub(r'&#\d+;', ' ', clean)
                    clean = re.sub(r'\s+', ' ', clean).strip()
                    if len(clean) > 5:
                        results.append(clean)
                    if len(results) >= 5:
                        break

            if not results:
                return {"success": False, "error": "未找到新闻结果"}

            summary = "\n".join(f"{i+1}. {r}" for i, r in enumerate(results[:5]))
            logger.info("[NEWS_QUERY] 搜索完成 | query=%s | 结果数=%d", clean_query, len(results[:5]))
            return {"success": True, "query": clean_query, "summary": summary}
        except Exception as e:
            logger.error("[NEWS_QUERY] 搜索失败: %s", e)
            return {"success": False, "error": str(e)}

    # ---- file_search：后端本地执行（WSL 可访问 /mnt/d/）----

    def _file_search_everything(args: dict) -> dict:
        """文件搜索，使用 Everything CLI 或 Python glob 降级"""
        query = args.get("query", "")
        import glob as glob_module
        results = []
        # 搜索常用位置
        search_paths = ["/mnt/d/", "/mnt/c/Users/"]
        found = set()
        for sp in search_paths:
            for match in glob_module.glob(os.path.join(sp, f"*{query}*"), recursive=True):
                if match not in found:
                    found.add(match)
                    results.append(match)
                    if len(results) >= 10:
                        break
            if len(results) >= 10:
                break
        return {"query": query, "results": results[:10], "count": len(results[:10])}

    # ---- file_read：后端本地执行 ----

    def _file_read_content(args: dict) -> dict:
        """读取文件内容"""
        file_path = args.get("file_path", "")
        try:
            # 支持跨平台路径映射（WSL 路径转 Windows 路径）
            win_path = file_path.replace("/mnt/d/", "D:/").replace("/mnt/c/", "C:/")
            target_path = os.path.exists(file_path) and file_path or (
                os.path.exists(win_path) and win_path or file_path
            )
            with open(target_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read(5000)  # 限制读取大小
            return {"file_path": target_path, "content": content, "size": len(content)}
        except Exception as e:
            raise RuntimeError(f"文件读取失败: {e}")

    # ---- get_current_time：后端直接执行 ----

    def _wsl_to_win_path(wsl_path: str) -> str:
        """Convert WSL path to Windows path: /mnt/d/xxx -> D:\\xxx"""
        if wsl_path.startswith("/mnt/") and len(wsl_path) > 6:
            drive = wsl_path[5].upper()
            rest = wsl_path[6:].replace("/", "\\")
            return f"{drive}:{rest}"
        return wsl_path

    def _volume_get(args: dict) -> dict:
        """查询当前系统音量（通过 WSL 调用 Windows PowerShell 脚本）"""
        import subprocess, os
        ps_path = _wsl_to_win_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "volume_control.ps1"))
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", ps_path, "get"],
                capture_output=True, timeout=15
            )
            stdout = result.stdout.decode('utf-8', errors='replace').strip()
            # 只取最后一行数字（过滤 banner 等杂讯）
            for line in reversed(stdout.splitlines()):
                line = line.strip()
                if line.isdigit():
                    return {"success": True, "level": int(line)}
            return {"success": False, "error": f"无法解析音量值: {stdout[-100:]}"}
        except Exception as e:
            logger.warning(f"[VOLUME] 查询音量失败: {e}")
            return {"success": False, "error": str(e)}

    def _volume_set(args: dict) -> dict:
        """设置系统音量（通过 WSL 调用 Windows PowerShell 脚本）"""
        import subprocess, os
        level = int(args.get("level", 50))
        level = max(0, min(100, level))
        ps_path = _wsl_to_win_path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "volume_control.ps1"))
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NoLogo", "-ExecutionPolicy", "Bypass", "-File", ps_path, "set", str(level)],
                capture_output=True, timeout=15
            )
            stdout = result.stdout.decode('utf-8', errors='replace').strip()
            if "OK:" in stdout:
                actual_level = int(stdout.split("OK:")[1].strip())
                return {"success": True, "level": actual_level}
            elif "OK" in stdout:
                return {"success": True, "level": level}
            else:
                stderr = result.stderr.decode('utf-8', errors='replace').strip()
                logger.warning(f"[VOLUME] 设置失败: stdout={stdout[:100]} stderr={stderr[:100]}")
                return {"success": False, "error": stderr[:200]}
        except Exception as e:
            logger.warning(f"[VOLUME] 设置音量失败: {e}")
            return {"success": False, "error": str(e)}

    def _get_current_time(args: dict) -> dict:
        """获取当前日期和时间"""
        from datetime import datetime
        now = datetime.now()
        return {
            "date": now.strftime("%Y年%m月%d日"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"][now.weekday()],
            "iso": now.isoformat(),
        }

    def _weather_query(args: dict) -> dict:
        """查询天气，使用 Open-Meteo 免费 API（无需 API key）"""
        import urllib.request, urllib.parse, json as json_mod

        city = args.get("city", "").strip()
        if not city:
            return {"success": False, "error": "城市名称为空"}

        try:
            # 1. 地理编码：城市名 → 坐标
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(city)}&count=1&language=zh"
            req = urllib.request.Request(geo_url, headers={"User-Agent": "Jarvis/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                geo_data = json_mod.loads(resp.read().decode("utf-8"))

            if not geo_data.get("results"):
                return {"success": False, "error": f"未找到城市: {city}"}

            loc = geo_data["results"][0]
            lat = loc["latitude"]
            lon = loc["longitude"]
            loc_name = loc.get("name", city)
            country = loc.get("country", "")
            admin1 = loc.get("admin1", "")

            # 2. 天气预报
            weather_url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
                f"&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
                f"&timezone=Asia%2FShanghai&forecast_days=3"
            )
            req2 = urllib.request.Request(weather_url, headers={"User-Agent": "Jarvis/1.0"})
            with urllib.request.urlopen(req2, timeout=10) as resp2:
                w_data = json_mod.loads(resp2.read().decode("utf-8"))

            # WMO 天气码映射
            wmo_map = {
                0: "晴", 1: "主要晴朗", 2: "部分多云", 3: "阴",
                45: "雾", 48: "雾凇",
                51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
                56: "冻毛毛雨", 57: "强冻毛毛雨",
                61: "小雨", 63: "中雨", 65: "大雨",
                66: "冻雨", 67: "强冻雨",
                71: "小雪", 73: "中雪", 75: "大雪",
                77: "雪粒",
                80: "小阵雨", 81: "阵雨", 82: "强阵雨",
                85: "小阵雪", 86: "强阵雪",
                95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
            }

            cur = w_data.get("current", {})
            daily = w_data.get("daily", {})

            cur_temp = cur.get("temperature_2m", 0)
            cur_humidity = cur.get("relative_humidity_2m", 0)
            cur_feels = cur.get("apparent_temperature", cur_temp)
            cur_precip = cur.get("precipitation", 0)
            cur_code = cur.get("weather_code", 0)
            cur_wind = cur.get("wind_speed_10m", 0)
            cur_desc = wmo_map.get(cur_code, "未知")

            # 未来3天预报
            forecast_parts = []
            dates = daily.get("time", [])
            codes = daily.get("weather_code", [])
            t_max = daily.get("temperature_2m_max", [])
            t_min = daily.get("temperature_2m_min", [])
            precip_sum = daily.get("precipitation_sum", [])

            from datetime import datetime as dt
            for i in range(min(3, len(dates))):
                d = dates[i]
                try:
                    wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][dt.fromisoformat(d).weekday()]
                except Exception:
                    wd = ""
                desc = wmo_map.get(codes[i], "未知")
                forecast_parts.append(f"{d}({wd}): {desc}, {t_min[i]:.0f}~{t_max[i]:.0f}°C, 降水{precip_sum[i]:.1f}mm")

            full_loc = f"{loc_name}{('，' + admin1) if admin1 and admin1 != loc_name else ''}{('，' + country) if country else ''}"
            summary = (
                f"{full_loc} 当前天气：{cur_desc}，{cur_temp:.1f}°C（体感{cur_feels:.0f}°C），"
                f"湿度{cur_humidity}%，风速{cur_wind:.1f}km/h，降水量{cur_precip}mm。\n"
                f"未来3天预报：\n" + "\n".join(forecast_parts)
            )

            logger.info("[WEATHER] 查询完成 | city=%s | temp=%.1f°C | desc=%s", city, cur_temp, cur_desc)
            return {"success": True, "city": city, "summary": summary}

        except Exception as e:
            logger.error("[WEATHER] 查询失败: %s", e)
            return {"success": False, "error": str(e)}

    def _hackernews_query(args: dict) -> dict:
        """查询 Hacker News 热榜，使用 Algolia API（免费、无需 API key）"""
        import urllib.request, urllib.parse, json as json_mod

        query = args.get("query", "").strip()

        try:
            if query:
                # 按关键词搜索（按热度排序）
                url = f"https://hn.algolia.com/api/v1/search?query={urllib.parse.quote(query)}&tags=story&hitsPerPage=5"
            else:
                # 热榜（按分数排序，取前5）
                url = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=5"

            req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json_mod.loads(resp.read().decode("utf-8"))

            hits = data.get("hits", [])
            if not hits:
                return {"success": False, "error": "未找到新闻"}

            results = []
            for h in hits[:5]:
                title = h.get("title", "")
                url_link = h.get("url", "")
                points = h.get("points", 0)
                author = h.get("author", "")
                num_comments = h.get("num_comments", 0)
                created = h.get("created_at", "")[:10]

                entry = f"{title} | 分数:{points} | 评论:{num_comments} | 作者:{author} | 日期:{created}"
                if url_link:
                    entry += f" | 链接:{url_link}"
                results.append(entry)

            summary = "\n".join(f"{i+1}. {r}" for i, r in enumerate(results))
            logger.info("[HACKERNEWS] 查询完成 | query=%s | 结果数=%d", query or "热榜", len(results))
            return {"success": True, "query": query or "热榜", "summary": summary}

        except Exception as e:
            logger.error("[HACKERNEWS] 查询失败: %s", e)
            return {"success": False, "error": str(e)}

    # 注册
    _local_executors = {
        "device_control.volume_up": (_device_control, "device_control"),
        "device_control.volume_down": (_device_control, "device_control"),
        "device_control.volume_mute": (_device_control, "device_control"),
        "device_control.volume_set": (_volume_set, "device_control"),
        "device_control.volume_get": (_volume_get, "device_control"),
        "device_control.media_play_pause": (_device_control, "device_control"),
        "device_control.media_next": (_device_control, "device_control"),
        "device_control.media_prev": (_device_control, "device_control"),
        "device_control.window_minimize": (_device_control, "device_control"),
        "device_control.window_maximize": (_device_control, "device_control"),
        "device_control.system_lock": (_device_control, "device_control"),
        "app_management.app_launch": (_app_launch, "app_management"),
        "app_management.app_close": (_app_close, "app_management"),
        "web_search.web_search": (_web_search, "web_search"),
        "web_search.open_url": (_open_url, "web_search"),
        "web_search.web_query": (_web_query, "web_search"),
        "web_search.news_query": (_news_query, "web_search"),
        "file_search.file_search_everything": (_file_search_everything, "file_search"),
        "file_read.file_read_content": (_file_read_content, "file_read"),
        "system_info.get_current_time": (_get_current_time, "system_info"),
        "web_search.weather_query": (_weather_query, "web_search"),
        "web_search.hackernews_query": (_hackernews_query, "web_search"),
    }

    for key, (fn, _cat) in _local_executors.items():
        ACTION_EXECUTORS[key] = fn

    logger.info("[EXEC] 已注册 %d 个 Function Calling 执行器", len(ACTION_EXECUTORS))


def register_executor(category: str, action: str, fn):
    key = f"{category}.{action}"
    ACTION_EXECUTORS[key] = fn


async def execute_function_call(name: str, args: dict, websocket=None) -> dict:
    """执行白名单校验后的 Function Calling，支持路由到前端执行"""
    category_map = {
        "device_control": "device_control",
        "app_launch": "app_management",
        "app_close": "app_management",
        "web_search": "web_search",
        "web_query": "web_search",
        "news_query": "web_search",
        "weather_query": "web_search",
        "hackernews_query": "web_search",
        "open_url": "web_search",
        "file_search_everything": "file_search",
        "file_read_content": "file_read",
        "get_current_time": "system_info",
    }
    category = category_map.get(name, "")
    action_name = args.get("action", name)

    logger.info("[EXEC] FC 执行 | name=%s | category=%s | action=%s | args=%s", name, category, action_name, args)

    ok, reason = validate_action(action_name, category, args)
    if not ok:
        logger.warning("[EXEC] 权限拦截 | action=%s | category=%s | reason=%s", action_name, category, reason)
        return {"success": False, "error": f"权限不足: {reason}"}

    # 清理 URL 参数中的 Markdown 符号和多余空格（LLM 有时会返回 `url` 或 'url'）
    if action_name == "open_url" and "url" in args:
        args["url"] = args["url"].strip().strip('`').strip("'").strip('"').strip().strip('`').strip()

    key = f"{category}.{action_name}"
    executor = ACTION_EXECUTORS.get(key)
    if executor:
        try:
            result = executor(args)
            # 检查是否需要路由到前端执行
            if isinstance(result, dict) and result.get("__route") == "frontend" and websocket:
                # 将操作指令下发给前端 Tauri 执行（await 确保消息顺序）
                _frontend_action_id = f"fe_{int(time.time())}"

                # 对于 app_launch，创建 Future 等待前端结果（用于自动降级）
                future = None
                if action_name == "app_launch":
                    future = asyncio.get_event_loop().create_future()
                    _action_result_waiters[_frontend_action_id] = future

                await safe_send(websocket, {
                    "type": "action_execute",
                    "data": {
                        "action_id": _frontend_action_id,
                        "action_name": action_name,
                        "action_category": result["category"],
                        "params": result["params"],
                        "timestamp": now_ms(),
                    },
                })
                logger.info("[EXEC] 已下发至前端 | action=%s | id=%s", action_name, _frontend_action_id)

                # app_launch 等待前端结果（超时 15 秒），其他 FC 不等待
                if action_name == "app_launch" and future:
                    try:
                        frontend_result = await asyncio.wait_for(future, timeout=15.0)
                        logger.info("[EXEC] app_launch 前端结果 | success=%s", frontend_result.get("success"))
                        return frontend_result
                    except asyncio.TimeoutError:
                        logger.warning("[EXEC] app_launch 前端执行超时 | id=%s", _frontend_action_id)
                        # 超时不视为失败：前端可能已成功打开应用，只是 action_result 因 WebSocket 断开未回传
                        return {"success": True, "result": "已下发至前端执行（等待确认超时）", "__routed": True}
                    finally:
                        _action_result_waiters.pop(_frontend_action_id, None)
                else:
                    return {"success": True, "result": f"已下发至前端执行: {action_name}", "__routed": True}
            logger.info("[EXEC] 本地执行完成 | key=%s | result=%s", key, str(result)[:200])
            return {"success": True, "result": result}
        except Exception as e:
            logger.error("[EXEC] 执行异常 | key=%s | error=%s", key, e)
            return {"success": False, "error": str(e)}

    logger.error("[EXEC] 执行器未注册 | key=%s | 已注册: %s", key, list(ACTION_EXECUTORS.keys()))
    return {"success": False, "error": f"执行器未注册: {key}"}


# ============================================================
# 唤醒词检测
# ============================================================

WAKE_WORDS = [
    "jarvis", "javis", "jarvice", "jovis", "chavis", "travis", "dvis",
    "贾维斯", "加维斯", "贾维丝", "加维丝",
    "乔维斯", "佐维斯", "焦维斯", "嘉维斯", "查维斯",
    "叫微思", "教微思", "贾维思", "加维思",
]
# 模糊匹配子串：识别结果包含这些子串且长度 <= 8 即视为唤醒
WAKE_FUZZY_SUBSTRINGS = ["vis", "维斯", "微思", "维思", "javi", "jovi"]
WAKE_FUZZY_MAX_LEN = 8
# SenseVoice 热词：提高唤醒词识别准确率
WAKE_HOTWORD = "贾维斯 JARVIS 乔维斯 佐维斯 加维斯"
# 普通 STT 热词：提高常见专有名词识别准确率（避免"世界杯"→"十杯"等错误）
STT_HOTWORD = "世界杯 奥运会 NBA CBA 欧洲杯 亚洲杯 英超 西甲 德甲 意甲 法甲 亚运会 欧冠"

# 待机触发词：用户说出这些词后进入待机状态（State 0）
STANDBY_WORDS = ["待机", "关闭", "standby", "mute", "close down"]


def check_wake_word(text: str) -> bool:
    """检查 STT 文本是否包含唤醒词（精确匹配 + 模糊子串匹配）"""
    text_lower = text.lower().strip()

    # 精确匹配
    for word in WAKE_WORDS:
        if word in text_lower:
            return True

    # 模糊匹配：短文本中包含 vis/维斯/微思 等子串
    if len(text_lower) <= WAKE_FUZZY_MAX_LEN:
        for sub in WAKE_FUZZY_SUBSTRINGS:
            if sub in text_lower:
                return True

    return False


# State 1 VAD 静音检测参数
VAD_THRESHOLD = 0.008  # RMS 阈值（比前端唤醒词检测更低，因为 State 1 已经在录音）
VAD_SILENCE_SEC = 2.0  # 静音超过 2 秒自动停止录音


# ============================================================
# WebSocket 连接处理
# ============================================================

async def handle_connection(websocket, path=None):
    """处理单个 WebSocket 连接的全生命周期"""
    session_id = f"sess_{id(websocket)}"
    logger.info("[WS] 新连接 | Session: %s | Path: %s", session_id, path)

    # 发送 connection_established
    await safe_send(websocket, {
        "type": "connection_established",
        "data": {"session_id": session_id, "server_version": "1.0.0"},
    })

    # 推送初始状态
    await safe_send(websocket, {
        "type": "state_change",
        "data": {"state": 0, "prev_state": None, "timestamp": now_ms(), "payload": {"idle_reason": "startup"}},
    })

    # 推送系统状态
    if engine:
        await safe_send(websocket, {"type": "system_status", "data": engine.get_system_status()})

    audio_buffer = bytearray()
    recording = False
    processing = False  # 处理冷却锁，防止循环
    last_process_time = 0.0
    last_voice_time = 0.0  # State 1 VAD：最后一次检测到语音的时间
    wake_word_detecting = False  # 唤醒词检测锁，防止重复触发
    continuation_mode = False  # 延续对话模式：TTS 回答后等待用户追问（不自动超时，仅待机词退出）

    # ---- 连接建立时后台预加载 STT 引擎（用于唤醒词检测）----
    if engine and engine.stt and not engine.stt._loaded:
        threading.Thread(target=engine.stt.load, daemon=True).start()
        logger.info("[STT] 后台预加载已启动（用于唤醒词检测）")

    async def trigger_wake_word():
        """唤醒词触发后的流程：State 0 → State 3（TTS 播报）→ State 1（聆听用户指令）"""
        nonlocal recording, last_voice_time

        import random
        replies = ["Yes?", "At your service", "Sir?", "How can I help?", "Standing by"]
        reply = random.choice(replies)

        logger.info("[WAKE] 唤醒成功，播报: %s", reply)

        # State 0 → State 3（播报）
        await safe_send(websocket, {
            "type": "state_change",
            "data": {"state": 3, "prev_state": 0, "timestamp": now_ms(),
                     "payload": {"mode": "speaking", "tts_text": reply, "action": None}},
        })

        # TTS 合成并播报（清理 emoji）
        reply = re.sub(
            r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
            r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U000024FF'
            r'\U0001F200-\U0001F251\U0000FE00-\U0000FE0F\U0001F900-\U0001F9FF'
            r'\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF]+',
            '', reply
        ).strip()
        await synthesize_and_stream(websocket, reply)

        # State 3 → State 1（聆听用户指令）
        await safe_send(websocket, {
            "type": "state_change",
            "data": {"state": 1, "prev_state": 3, "timestamp": now_ms(),
                     "payload": {"wake_word": "jarvis", "vad_enabled": True, "vad_silence_threshold_ms": 2000}},
        })

        # 进入录音状态，初始化 VAD 计时器
        # 设置 3 秒宽限期，让用户有时间开始说话（避免 TTS 播报后立即触发静音检测）
        recording = True
        last_voice_time = time.time() + 3.0
        audio_buffer.clear()
        ensure_stt_loaded()

    try:
        async for raw_msg in websocket:
            try:
                msg = json.loads(raw_msg)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")
            msg_data = msg.get("data", {})

            # ---- 心跳 ----
            if msg_type == "ping":
                await safe_send(websocket, {"type": "pong", "data": {"timestamp": msg_data.get("timestamp", now_ms())}})
                continue

            # ---- 关闭 ----
            if msg_type == "close":
                logger.info("[WS] 客户端主动关闭 | Session: %s", session_id)
                break

            # ---- 音频分块（前端 → 后端）----
            # State 1 录音中：累加音频 + VAD 静音检测
            if msg_type == "audio_chunk":
                if recording and not processing:
                    chunk_b64 = msg_data.get("audio_base64", "")
                    if chunk_b64:
                        chunk_bytes = base64.b64decode(chunk_b64)
                        audio_buffer.extend(chunk_bytes)

                        # VAD：计算当前 chunk 的 RMS
                        rms = compute_rms(chunk_bytes)
                        if rms > VAD_THRESHOLD:
                            last_voice_time = time.time()
                            # 检测到语音，退出延续对话模式
                            if continuation_mode:
                                continuation_mode = False
                        elif last_voice_time > 0 and (time.time() - last_voice_time) > VAD_SILENCE_SEC:
                            # 静音超过阈值
                            if continuation_mode and len(audio_buffer) < 32000:
                                # 延续对话模式下音频太短（<1秒），认为用户没说话
                                # 不回到待机，清空 buffer 继续聆听（仅待机词可退出）
                                logger.info("[WS] 延续对话无语音输入，继续聆听")
                                audio_buffer.clear()
                                last_voice_time = time.time() + 3.0  # 重置宽限期
                            else:
                                # 正常处理
                                logger.info("[VAD] 检测到静音 %.1f 秒，自动停止录音", VAD_SILENCE_SEC)
                                recording = False
                                processing = True
                                last_process_time = time.time()
                                should_continue = await process_audio(websocket, bytes(audio_buffer), session_id)
                                audio_buffer.clear()
                                processing = False
                                last_voice_time = 0.0
                                if should_continue:
                                    # process_audio 回答后进入延续对话模式
                                    recording = True
                                    continuation_mode = True
                                    last_voice_time = time.time() + 5.0  # 5 秒宽限期（防止 TTS 回声）
                                    logger.info("[WS] VAD 自动处理完成，进入延续对话模式")
                                else:
                                    if continuation_mode:
                                        # 延续对话模式下无有效输入（空文本/过短），继续聆听
                                        recording = True
                                        last_voice_time = time.time() + 3.0
                                        logger.info("[WS] 延续对话模式，继续聆听")
                                    else:
                                        recording = False
                                        logger.info("[WS] VAD 自动处理完成，回到待机")
                continue

            # ---- 录音结束（旧路径，兼容性保留）----
            if msg_type == "audio_end":
                if recording and not processing:
                    recording = False
                    processing = True
                    last_process_time = time.time()
                    should_continue = await process_audio(websocket, bytes(audio_buffer), session_id)
                    audio_buffer.clear()
                    processing = False
                    if should_continue:
                        recording = True
                        continuation_mode = True
                        last_voice_time = time.time() + 5.0  # 5 秒宽限期
                continue

            # ---- 麦克风响度 ----
            if msg_type == "mic_volume_rms":
                # 后端可利用此数据做 VAD 辅助，当前仅记录
                continue

            # ---- 唤醒词检测音频（State 0 前端 VAD 预过滤后发送）----
            if msg_type == "wake_word_audio":
                if not recording and not processing and not wake_word_detecting:
                    wake_word_detecting = True
                    try:
                        audio_b64 = msg_data.get("audio_base64", "")
                        if not audio_b64:
                            continue

                        audio_bytes = base64.b64decode(audio_b64)
                        if len(audio_bytes) < 3200:  # 至少 0.1 秒
                            continue

                        # STT 识别
                        stt_text = ""
                        if engine and engine.stt and engine.stt._loaded:
                            import tempfile
                            import soundfile as sf
                            import numpy as np

                            pcm_array = np.frombuffer(audio_bytes, dtype=np.int16)
                            tmp_wav = os.path.join(tempfile.gettempdir(), f"jarvis_wake_{session_id}.wav")
                            sf.write(tmp_wav, pcm_array, samplerate=16000)

                            try:
                                result = engine.stt.transcribe(tmp_wav, language="auto", hotword=WAKE_HOTWORD)
                                stt_text = result.get("text", "")
                            except Exception as e:
                                logger.error("[WAKE] STT 识别失败: %s", e)
                                continue
                        else:
                            logger.warning("[WAKE] STT 引擎未加载，跳过唤醒词检测")
                            continue

                        # 唤醒词匹配
                        if check_wake_word(stt_text):
                            logger.info("[WAKE] 检测到唤醒词 | STT: %s", stt_text)
                            await trigger_wake_word()
                        else:
                            logger.debug("[WAKE] 非唤醒词 | STT: %s", stt_text)
                    except Exception as e:
                        logger.error("[WAKE] 唤醒词检测异常: %s", e)
                    finally:
                        wake_word_detecting = False
                continue

            # ---- 前端主动请求开始录音（手动点击备用模式）----
            if msg_type == "start_recording":
                if not recording and not processing:
                    # 冷却期检查：距上次处理至少间隔 2 秒
                    cooldown = 2.0
                    elapsed = time.time() - last_process_time
                    if last_process_time > 0 and elapsed < cooldown:
                        logger.info("[WS] 录音请求被冷却拦截 | 剩余: %.1fs", cooldown - elapsed)
                        continue
                    recording = True
                    last_voice_time = time.time()  # 初始化 VAD 计时器
                    audio_buffer.clear()
                    ensure_stt_loaded()
                    await safe_send(websocket, {
                        "type": "state_change",
                        "data": {"state": 1, "prev_state": 0, "timestamp": now_ms(),
                                 "payload": {"wake_word": "manual", "vad_enabled": True, "vad_silence_threshold_ms": 2000}},
                    })
                continue

            # ---- 前端主动停止录音 ----
            if msg_type == "stop_recording":
                if recording and not processing:
                    recording = False
                    processing = True  # 加锁，防止循环
                    last_process_time = time.time()
                    should_continue = await process_audio(websocket, bytes(audio_buffer), session_id)
                    audio_buffer.clear()
                    processing = False
                    if should_continue:
                        recording = True
                        continuation_mode = True
                        last_voice_time = time.time() + 5.0  # 5 秒宽限期
                        logger.info("[WS] 处理完成，进入延续对话模式")
                    else:
                        if continuation_mode:
                            recording = True
                            last_voice_time = time.time() + 3.0
                            logger.info("[WS] 延续对话模式，继续聆听")
                        else:
                            logger.info("[WS] 处理完成，回到待机")
                continue

            # ---- 清空短期记忆 ----
            if msg_type == "clear_memory":
                if engine and engine.short_term:
                    engine.short_term.clear()
                    logger.info("[WS] 短期记忆已清空")
                continue

            # ---- 前端 action_execute 执行结果回传 ----
            if msg_type == "action_result":
                _action_id = msg_data.get("action_id")
                _success = msg_data.get("success", False)
                _result = msg_data.get("result")
                _error = msg_data.get("error")
                logger.info("[WS] 收到前端结果 | id=%s | success=%s", _action_id, _success)
                if _action_id in _action_result_waiters:
                    future = _action_result_waiters.pop(_action_id)
                    if not future.done():
                        future.set_result({"success": _success, "result": _result, "error": _error})
                continue

    except websockets.exceptions.ConnectionClosed:
        logger.info("[WS] 连接断开 | Session: %s", session_id)
    except Exception as e:
        logger.error("[WS] 连接异常 | Session: %s | Error: %s", session_id, e)
        traceback.print_exc()
    finally:
        logger.info("[WS] 连接清理 | Session: %s", session_id)


async def process_audio(websocket, audio_bytes: bytes, session_id: str) -> bool:
    """处理音频：STT → LLM → TTS，返回 True 表示应进入延续对话模式"""
    """全双工流水线核心：STT → 记忆检索 → LLM → TTS → 音频回传"""
    stt_text = ""
    emotion_tags = []

    # ---- T1: STT 语音识别 ----
    if engine and engine.stt and engine.stt._loaded and len(audio_bytes) > 0:
        import tempfile
        import soundfile as sf
        import numpy as np

        # 将 PCM int16 转为临时 wav 文件
        pcm_array = np.frombuffer(audio_bytes, dtype=np.int16)
        tmp_wav = os.path.join(tempfile.gettempdir(), f"jarvis_stt_{session_id}.wav")
        sf.write(tmp_wav, pcm_array, samplerate=16000)

        try:
            # SenseVoice 自动检测语言（中/英/日/韩/粤），无需手动指定
            result = engine.stt.transcribe(tmp_wav, language="auto", hotword=STT_HOTWORD)
            stt_text = result.get("text", "")
            emotion_tags = result.get("emotion_tags", [])
        except Exception as e:
            logger.error("[STT] 识别失败: %s", e)
            stt_text = ""
    else:
        # 无 STT 引擎时返回空文本（触发 State 0 回退）
        stt_text = ""

    if not stt_text.strip():
        await safe_send(websocket, {
            "type": "state_change",
            "data": {"state": 0, "prev_state": 1, "timestamp": now_ms(), "payload": {"idle_reason": "timeout"}},
        })
        return False

    # 过滤极短文本（TTS 回声或环境噪音导致的误识别）
    chinese_chars = len(re.findall(r'[\u4e00-\u9fff]', stt_text))
    if chinese_chars < 2 and len(stt_text.strip()) < 4:
        logger.info("[STT] 文本过短，忽略: %s", stt_text)
        await safe_send(websocket, {
            "type": "state_change",
            "data": {"state": 0, "prev_state": 1, "timestamp": now_ms(), "payload": {"idle_reason": "too_short"}},
        })
        return False

    # 待机词检测：用户说出待机触发词后进入 State 0
    _stby_text = stt_text.strip().lower()
    for _word in STANDBY_WORDS:
        if _word in _stby_text:
            logger.info("[STT] 检测到待机词: %s | 原文: %s", _word, stt_text)
            await safe_send(websocket, {
                "type": "state_change",
                "data": {"state": 3, "prev_state": 1, "timestamp": now_ms(),
                         "payload": {"mode": "speaking", "tts_text": "Entering standby", "action": None}},
            })
            await synthesize_and_stream(websocket, "Alright, entering standby mode.")
            await asyncio.sleep(1.0)
            await safe_send(websocket, {
                "type": "state_change",
                "data": {"state": 0, "prev_state": 3, "timestamp": now_ms(), "payload": {"idle_reason": "standby_command"}},
            })
            return False

    # ---- T2: 记忆检索 ----
    memories = []
    if engine and engine.memory and engine.memory._loaded:
        try:
            memories = engine.memory.retrieve(stt_text)
            if memories:
                await safe_send(websocket, {
                    "type": "memory_retrieved",
                    "data": {"query": stt_text, "matches": memories},
                })
        except Exception as e:
            logger.warning("[MEMORY] 检索失败: %s", e)

    # ---- T3: 云端推理 ----
    await safe_send(websocket, {
        "type": "state_change",
        "data": {"state": 2, "prev_state": 1, "timestamp": now_ms(),
                 "payload": {"stt_text": stt_text, "has_function_call": False}},
    })

    llm_result = await call_llm(stt_text, memories, emotion_tags)

    # 日志：LLM 返回类型和内容
    if llm_result.get("type") == "function_call":
        logger.info("[LLM] 返回 Function Call | name=%s | args=%s", llm_result.get("name"), llm_result.get("args"))
    else:
        logger.info("[LLM] 返回文本 | content=%.100s", llm_result.get("content", ""))

    if engine and engine.short_term:
        # 这些 FC 的用户消息不写入短期记忆（避免意图污染）
        _skip_memory_funcs = ("device_control", "web_search", "web_query", "open_url")
        is_skip = llm_result.get("type") == "function_call" and llm_result.get("name") in _skip_memory_funcs
        if not is_skip:
            engine.short_term.add("user", stt_text)

    # ---- Function Calling 分支 ----
    if llm_result.get("type") == "function_call":
        func_name = llm_result["name"]
        func_args = llm_result["args"]
        # 强制 app_launch 优先：LLM 直接返回 open_url 打开应用时，转为 app_launch
        if func_name == "open_url" and func_args.get("description"):
            _desc = func_args["description"]
            if _desc not in _app_web_urls:
                logger.info("[LLM] 拦截 open_url → app_launch | app=%s", _desc)
                func_name = "app_launch"
                func_args = {"app_name": _desc}
        # 从 args 中提取 action_name（用于 friendly_map 查找）
        action_name = func_args.get("action", func_name)
        action_id = f"act_{int(time.time())}"

        # 通知前端：操作开始
        await safe_send(websocket, {
            "type": "action_start",
            "data": {"action_id": action_id, "action_name": func_name,
                     "action_category": func_name, "params": func_args, "timestamp": now_ms()},
        })

        # 更新 State 2 payload
        await safe_send(websocket, {
            "type": "state_change",
            "data": {"state": 2, "prev_state": 2, "timestamp": now_ms(),
                     "payload": {"stt_text": stt_text, "has_function_call": True}},
        })

        # 执行操作
        exec_result = await execute_function_call(func_name, func_args, websocket)

        # 通知前端：操作结束
        await safe_send(websocket, {
            "type": "action_end",
            "data": {"action_id": action_id, "success": exec_result.get("success", False),
                     "result": exec_result.get("result"), "error": exec_result.get("error"), "timestamp": now_ms()},
        })

        memory_added = False
        reply_text = f"[工具已调用] {func_name}" if exec_result.get("success") else f"[工具调用失败] {func_name}: {exec_result.get('error', '未知错误')}"
        # app_launch 失败时自动降级为 open_url（先查缓存，再搜官网）
        if func_name == "app_launch" and not exec_result.get("success"):
            app_name = func_args.get("app_name", "")
            web_url = _get_app_web_url(app_name)
            if not web_url:
                # 用 web_query 搜索官网
                web_query_fn = ACTION_EXECUTORS.get("web_search.web_query")
                if web_query_fn:
                    search_result = web_query_fn({"query": f"{app_name} 官网网址"})
                    if search_result.get("success"):
                        summary = search_result.get("summary", "")
                        import re as re_mod
                        urls = re_mod.findall(r'https?://[a-zA-Z0-9\-._~:/?#\[\]@!$&\'()*+,;=%]+', summary)
                        if urls:
                            web_url = urls[0]
                            _save_app_web_url(app_name, web_url)
            if web_url:
                logger.info("[EXEC] app_launch 降级为 open_url | app=%s | url=%s", app_name, web_url)
                await execute_function_call("open_url", {"url": web_url, "description": app_name}, websocket)
                reply_text = f"电脑上未安装 {app_name}，已为你打开网页版"
                memory_added = False
            else:
                reply_text = f"电脑上未安装 {app_name}，也无法找到官网"
                memory_added = False
        # 对于 web_query / news_query，将搜索结果传给 LLM 二次调用，生成直接回答
        elif func_name in ("web_query", "news_query") and exec_result.get("success"):
            search_summary = exec_result.get("result", {}).get("summary", "")
            if search_summary:
                logger.info("[%s] 二次调用 LLM 总结搜索结果", func_name.upper())
                follow_up = await call_llm(stt_text, memories, emotion_tags, extra_context=search_summary, no_tools=True)
                if follow_up.get("type") == "text":
                    reply_text = follow_up.get("content", "抱歉，我无法找到相关信息。")
                else:
                    reply_text = "抱歉，我无法找到相关信息。"
                memory_added = False
            else:
                reply_text = "抱歉，未找到相关搜索结果。"
                memory_added = True
        # weather_query / hackernews_query 结果已格式化，直接作为回复
        elif func_name in ("weather_query", "hackernews_query") and exec_result.get("success"):
            result_data = exec_result.get("result", {})
            if isinstance(result_data, dict) and result_data.get("summary"):
                reply_text = result_data["summary"]
            else:
                reply_text = "抱歉，查询失败。"
            memory_added = False
        # 对于 get_current_time，直接用结果作为回复
        elif func_name == "get_current_time" and exec_result.get("success"):
            time_result = exec_result.get("result", {})
            if isinstance(time_result, dict):
                reply_text = f"{time_result.get('date', '')} {time_result.get('weekday', '')} {time_result.get('time', '')}"
            else:
                reply_text = str(time_result)
        elif action_name == "volume_get" and exec_result.get("success"):
            vol_result = exec_result.get("result", {})
            if isinstance(vol_result, dict) and "level" in vol_result:
                reply_text = f"当前音量为 {vol_result['level']}%"
            elif isinstance(vol_result, dict) and vol_result.get("success"):
                reply_text = f"当前音量为 {vol_result.get('level', '未知')}%"
            else:
                reply_text = f"当前音量: {vol_result}"
        # 对于 app_launch / app_close / device_control，短期记忆用标记，TTS 用友好文本
        elif exec_result.get("success"):
            friendly_map = {
                "app_launch": f"已打开 {func_args.get('app_name', '应用')}",
                "app_close": f"已关闭 {func_args.get('app_name', '应用')}",
                "web_search": f"正在搜索 {func_args.get('query', '')}",
                "open_url": f"正在打开 {func_args.get('description', func_args.get('url', '网页'))}",
                "volume_mute": "已静音",
                "volume_set": f"音量已设为 {exec_result.get('result', {}).get('level', func_args.get('level', 50)) if isinstance(exec_result.get('result'), dict) else func_args.get('level', 50)}",
                "volume_get": "当前音量查询中",
                "volume_up": "已调高音量",
                "volume_down": "已调低音量",
                "media_play_pause": "已切换播放",
                "media_prev": "已切换上一曲",
                "media_next": "已切换下一曲",
                "window_minimize": "已最小化窗口",
                "window_maximize": "已最大化窗口",
                "lock_screen": "已锁屏",
            }
            # 短期记忆存标记格式（防止 LLM 模仿 TTS 文本），TTS 用友好文本
            if engine and engine.short_term:
                # device_control / web_search / web_query / open_url 不写入短期记忆（避免参数污染和意图混淆）
                if func_name not in ("device_control", "web_search", "web_query", "open_url"):
                    mem_args = {k: v for k, v in func_args.items() if k in ("app_name", "query", "url", "description")}
                    engine.short_term.add("assistant", f"[工具已调用] {func_name}({mem_args})")
                    memory_added = True
                else:
                    memory_added = True  # 标记已处理，不写入
            reply_text = friendly_map.get(action_name, friendly_map.get(func_name, f"已执行: {func_name}"))
    else:
        reply_text = llm_result.get("content", "")
        memory_added = False

    if engine and engine.short_term and not memory_added:
        engine.short_term.add("assistant", reply_text)

    # ---- T4/T5: TTS 合成 + 音频流回传 ----
    await safe_send(websocket, {
        "type": "state_change",
        "data": {"state": 3, "prev_state": 2, "timestamp": now_ms(),
                 "payload": {"mode": "speaking", "tts_text": reply_text, "action": None}},
    })

    # 清理 emoji，防止 TTS 读出表情符号名称
    reply_text = re.sub(
        r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF'
        r'\U0001F1E0-\U0001F1FF\U00002702-\U000027B0\U000024C2-\U000024FF'
        r'\U0001F200-\U0001F251\U0000FE00-\U0000FE0F\U0001F900-\U0001F9FF'
        r'\U0001FA00-\U0001FA6F\U0001FA70-\U0001FAFF\U00002600-\U000026FF]+',
        '', reply_text
    ).strip()

    await synthesize_and_stream(websocket, reply_text)

    # 等待前端播放完毕（额外 1 秒缓冲）
    await asyncio.sleep(1.0)

    # 回到聆听状态（延续对话模式，等待用户追问）
    await safe_send(websocket, {
        "type": "state_change",
        "data": {"state": 1, "prev_state": 3, "timestamp": now_ms(), "payload": {"idle_reason": "continuation", "vad_enabled": True, "vad_silence_threshold_ms": 2000}},
    })
    return True


async def _stream_synthesize_and_send(websocket, text: str):
    """GPT-SoVITS 流式合成：边从 API 读取 chunk 边推送 WebSocket，降低首字延迟和内存峰值"""
    import asyncio
    chunk_index = 0
    last_sample_rate = 32000

    loop = asyncio.get_event_loop()

    def _sync_stream():
        return engine.tts.synthesize_stream(text)

    try:
        for pcm_chunk, sample_rate, is_first in engine.tts.synthesize_stream(text):
            if not pcm_chunk:
                continue

            last_sample_rate = sample_rate

            await safe_send(websocket, {
                "type": "tts_chunk",
                "data": {
                    "sample_rate": sample_rate, "channels": 1, "encoding": "pcm_int16",
                    "chunk_index": chunk_index, "audio_base64": base64.b64encode(pcm_chunk).decode(),
                    "is_final": False,
                },
            })

            # 推送 Volume RMS
            rms = compute_rms(pcm_chunk)
            freq = estimate_frequency(pcm_chunk, sample_rate)
            await safe_send(websocket, {
                "type": "volume_rms",
                "data": {"t": now_ms(), "v": round(rms, 3), "f": round(freq, 1)},
            })

            chunk_index += 1
            # 让出事件循环，避免阻塞 WebSocket 心跳
            await asyncio.sleep(0.001)

        # 发送最终标记
        await safe_send(websocket, {
            "type": "tts_chunk",
            "data": {
                "sample_rate": last_sample_rate, "channels": 1, "encoding": "pcm_int16",
                "chunk_index": chunk_index, "audio_base64": "",
                "is_final": True,
            },
        })
        # 最终静音 RMS
        await safe_send(websocket, {
            "type": "volume_rms",
            "data": {"t": now_ms(), "v": 0.0, "f": 0.0},
        })
        logger.info("[TTS] 流式推送完成 | chunks=%d | 采样率=%d", chunk_index, last_sample_rate)

    except Exception as e:
        logger.error("[TTS] 流式推送失败: %s", e)
        # 发送最终标记确保前端不卡在 Speaking 状态
        await safe_send(websocket, {
            "type": "tts_chunk",
            "data": {
                "sample_rate": last_sample_rate, "channels": 1, "encoding": "pcm_int16",
                "chunk_index": chunk_index, "audio_base64": "",
                "is_final": True,
            },
        })
        await safe_send(websocket, {
            "type": "volume_rms",
            "data": {"t": now_ms(), "v": 0.0, "f": 0.0},
        })


async def synthesize_and_stream(websocket, text: str):
    """TTS 流式合成并逐 chunk 回传 + Volume RMS 推送"""
    audio_data = None
    is_mp3 = False

    # 先尝试加载 TTS（懒加载），再检查是否可用
    if engine and engine.tts and not engine.tts._degraded:
        try:
            ensure_tts_loaded()
            if engine.tts._loaded:
                # 优先使用流式合成（GPT-SoVITS streaming_mode=3）
                if hasattr(engine.tts, 'synthesize_stream') and engine.tts.backend == "gpt_sovits":
                    await _stream_synthesize_and_send(websocket, text)
                    return
                audio_data = engine.tts.synthesize(text)
                if getattr(engine.tts, 'backend', '') == 'edge_tts' and audio_data:
                    is_mp3 = True
        except Exception as e:
            logger.error("[TTS] 加载或合成失败: %s", e)

    if audio_data is None and not (engine and engine.tts and engine.tts._loaded and engine.tts.backend == "gpt_sovits"):
        # 无 TTS 引擎时生成静音占位
        import numpy as np
        duration_sec = max(len(text) * 0.1, 1.0)
        samples = int(24000 * duration_sec)
        silence = np.zeros(samples, dtype=np.int16)
        audio_data = silence.tobytes()
        logger.info("[TTS] 使用静音占位 | 时长: %.1fs", duration_sec)

    if is_mp3:
        # MP3 格式：整体发送，前端用 AudioContext.decodeAudioData() 解码
        await safe_send(websocket, {
            "type": "tts_chunk",
            "data": {
                "sample_rate": 24000, "channels": 1, "encoding": "mp3",
                "chunk_index": 0, "audio_base64": base64.b64encode(audio_data).decode(),
                "is_final": True,
                "total_bytes": len(audio_data),
            },
        })
        # MP3 模拟 RMS 推送（基于文本长度估算波形）
        # Edge-TTS MP3 码率约 48kbps，时长 = 字节数 * 8 / 48000 = 字节数 / 6000
        duration_est = max(len(audio_data) / 6000.0, 1.5)
        steps = int(duration_est * 60)  # 60fps
        for s in range(steps):
            progress = s / max(steps, 1)
            # 简单的包络模拟：中间响，两头弱
            envelope = min(1.0, progress * 4) * min(1.0, (1 - progress) * 4)
            rms = envelope * 0.5 + (envelope * 0.3 * ((s * 7) % 100) / 100.0)
            freq = 200 + rms * 400
            await safe_send(websocket, {
                "type": "volume_rms",
                "data": {"t": now_ms(), "v": round(rms, 3), "f": round(freq, 1)},
            })
            # 每 3 秒发送 pong 保活，防止前端心跳超时断开
            if s > 0 and s % 180 == 0:
                await safe_send(websocket, {"type": "pong", "data": {"timestamp": now_ms()}})
            await asyncio.sleep(1.0 / 60.0)

        await safe_send(websocket, {
            "type": "volume_rms",
            "data": {"t": now_ms(), "v": 0.0, "f": 0.0},
        })
    else:
        # PCM int16 格式：分块流式回传（原有逻辑）
        # GPT-SoVITS 采样率 32000，Edge-TTS 降级/静音占位采样率 24000
        sample_rate = getattr(engine.tts, '_last_sample_rate', 24000) if engine and engine.tts else 24000
        chunk_size = 4096
        total_chunks = (len(audio_data) + chunk_size - 1) // chunk_size

        for i in range(total_chunks):
            start = i * chunk_size
            end = min(start + chunk_size, len(audio_data))
            chunk = audio_data[start:end]

            await safe_send(websocket, {
                "type": "tts_chunk",
                "data": {
                    "sample_rate": sample_rate, "channels": 1, "encoding": "pcm_int16",
                    "chunk_index": i, "audio_base64": base64.b64encode(chunk).decode(),
                    "is_final": i == total_chunks - 1,
                },
            })

            # 推送 Volume RMS
            rms = compute_rms(chunk)
            freq = estimate_frequency(chunk, sample_rate)
            await safe_send(websocket, {
                "type": "volume_rms",
                "data": {"t": now_ms(), "v": round(rms, 3), "f": round(freq, 1)},
            })

            # 模拟实时流控（约 16ms/chunk ≈ 60fps）
            await asyncio.sleep(0.016)

        # 最终静音 RMS
        await safe_send(websocket, {
            "type": "volume_rms",
            "data": {"t": now_ms(), "v": 0.0, "f": 0.0},
        })


# ============================================================
# OOM 测试后门
# ============================================================

def oom_test_trigger():
    """强制触发显存溢出警告，验证降级机制"""
    global OOM_TEST_MODE
    OOM_TEST_MODE = True
    logger.warning("[OOM TEST] ===== 强制 OOM 测试触发 =====")

    if engine and engine.vram_monitor:
        from ai_engine import VRAMStatus
        fake_status = VRAMStatus(total_mb=6144, used_mb=5600, free_mb=544, usage_percent=91.2)
        with engine.vram_monitor._lock:
            engine.vram_monitor._status = fake_status
            engine.vram_monitor._degraded = True
        engine.vram_monitor.on_degrade(fake_status) if engine.vram_monitor.on_degrade else None
        # 直接调用引擎降级
        if engine.tts:
            engine.tts.degrade()

    logger.warning("[OOM TEST] 降级机制已触发，TTS 已挂起")


def oom_test_recover():
    """恢复 OOM 测试"""
    global OOM_TEST_MODE
    OOM_TEST_MODE = False
    logger.info("[OOM TEST] ===== OOM 测试恢复 =====")

    if engine and engine.vram_monitor:
        from ai_engine import VRAMStatus
        fake_status = VRAMStatus(total_mb=6144, used_mb=3500, free_mb=2644, usage_percent=57.0)
        with engine.vram_monitor._lock:
            engine.vram_monitor._status = fake_status
            engine.vram_monitor._degraded = False
        if engine.tts:
            engine.tts.recover()

    logger.info("[OOM TEST] TTS 已恢复 GPU 推理")


# ============================================================
# 工具函数
# ============================================================

async def safe_send(websocket, msg: dict):
    try:
        await websocket.send(json.dumps(msg))
    except websockets.exceptions.ConnectionClosed:
        pass
    except Exception as e:
        logger.warning("[WS] 发送失败: %s", e)


def now_ms() -> int:
    return int(time.time() * 1000)


# ============================================================
# 主启动
# ============================================================

async def main():
    global engine

    logger.info("=" * 60)
    logger.info("  Jarvis Assistant Backend v1.0")
    logger.info("  WebSocket: ws://%s:%d/ws", WS_HOST, WS_PORT)
    logger.info("  LLM: %s @ %s", LLM_MODEL, LLM_API_BASE)
    logger.info("=" * 60)

    init_engine()

    # 启动 WebSocket 服务
    async with serve(handle_connection, WS_HOST, WS_PORT):
        logger.info("[MAIN] WebSocket 服务已启动 | 监听: %s:%d", WS_HOST, WS_PORT)
        logger.info("[MAIN] OOM 测试后门: main.py 中调用 oom_test_trigger() / oom_test_recover()")
        await asyncio.Future()  # 永久运行


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("[MAIN] 收到中断信号，正在关闭...")
        if engine:
            engine.shutdown()
        logger.info("[MAIN] 已关闭")
