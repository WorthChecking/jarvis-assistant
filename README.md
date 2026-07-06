# JARVIS - PC 桌面端智能悬浮窗助手

基于 Three.js + Tauri + Python 的 PC 桌面端智能悬浮窗助手，集成语音识别（STT）、大语言模型（LLM）、语音合成（TTS）与三维粒子可视化。

## ✨ 功能特性

- **悬浮窗交互**：500×500 透明窗口，3D 粒子球面可视化（4 状态机：Standby / Listening / Processing / Speaking）
- **语音识别（STT）**：Faster-Whisper / SenseVoice-Small，支持热词增强
- **大语言模型（LLM）**：DeepSeek API，支持 Function Calling（天气查询、新闻搜索、HackerNews、应用启动等）
- **语音合成（TTS）**：GPT-SoVITS API，J.a.r.v.i.s 音色，流式合成低延迟
- **安全拦截器**：AOP 拦截危险文件操作（os.remove / shutil.rmtree 等），保护系统核心文件
- **显存管理**：RTX 3060 6GB 适配，int8/float16 量化，VRAM 监控守护线程，OOM 降级策略

## 🛠️ 技术栈

| 层级 | 技术 |
|---|---|
| 前端 | Vue 3 + TypeScript + Three.js (WebGL) + Tauri v1 |
| 后端 | Python + WebSocket + Faster-Whisper + GPT-SoVITS API |
| AI 模型 | DeepSeek (LLM) / SenseVoice-Small (STT) / GPT-SoVITS (TTS) |
| 向量数据库 | ChromaDB |

## 📦 项目结构

```
Jarvis_Assistant/
├── backend/                    # Python 后端
│   ├── main.py                 # WebSocket 服务入口
│   ├── ai_engine.py            # LLM 引擎与 Function Calling
│   ├── security_interceptor.py # 安全拦截器（AOP）
│   ├── app_web_urls.json       # 应用启动配置
│   └── requirements.txt
├── frontend/                   # Vue 3 + Tauri 前端
│   ├── src/
│   │   ├── engine/ParticleEngine.ts   # Three.js 粒子渲染引擎
│   │   ├── components/                # Vue 组件
│   │   ├── audio/AudioPipeline.ts     # 音频采集与播放
│   │   ├── ws/WSClient.ts             # WebSocket 客户端
│   │   └── state/StateMachine.ts      # 状态机
│   └── src-tauri/              # Tauri 配置
├── *.md                        # 设计文档与开发经验
└── 完整流程.txt                # 原始 Three.js 粒子设计基准
```

## 🚀 快速开始

### 前置条件

- Node.js 18+ / Python 3.10+ / Rust（Tauri v1）
- NVIDIA GPU（推荐 RTX 3060 6GB+）
- GPT-SoVITS API（独立运行，默认端口 9880）

### 后端

```bash
cd backend
pip install -r requirements.txt
# 配置 .env（API 密钥等）
python main.py
```

### 前端

```bash
cd frontend
npm install
npm run tauri dev
```

## 🔒 安全红线

- 严禁修改/删除系统核心文件（C:\Windows、C:\Program Files）
- 文件操作通过安全拦截器白名单执行
- 显存强制 int8/float16 量化与释放策略

## 📄 文档

- [PRD & 技术架构说明书](PC桌面端智能悬浮窗助手产品需求与技术架构说明书（PRD%20%26%20Architecture%20V1.0）.md)
- [前端开发踩坑与经验总结](前端开发踩坑与经验总结.md)
- [通信协议](communication_protocol.md)

## 📝 License

MIT
