<div align="center">

# JARVIS

### PC 桌面端智能悬浮窗助手

基于 Three.js WebGL 粒子可视化 + Tauri + Python 的全栈语音 AI 助手，集成 STT 语音识别、LLM 大模型推理、TTS 语音合成与三维粒子状态机。

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-Windows-blue.svg)]()
[![Frontend](https://img.shields.io/badge/Frontend-Vue%203%20%2B%20Tauri-42b883.svg)]()
[![Backend](https://img.shields.io/badge/Backend-Python%203.10%2B-3776AB.svg)]()
[![Rendering](https://img.shields.io/badge/Rendering-Three.js%20WebGL-000000.svg)]()
[![PRs Welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)
[![Made with ❤️](https://img.shields.io/badge/Made%20with-%E2%9D%A4-red.svg)]()

</div>

---

## 📖 项目简介

JARVIS 是一款 PC 桌面端智能悬浮窗助手，灵感来自钢铁侠的 J.A.R.V.I.S.。它通过 500×500 透明悬浮窗呈现 3D 粒子可视化交互界面，背后串联语音识别、大语言模型推理、语音合成三大 AI 能力，并通过 WebSocket 实现前后端实时通信。

项目针对 **RTX 3060 6GB** 显存做了严格优化，采用 int8/float16 量化与显存释放策略，并在文件操作层接入 AOP 安全拦截器，确保系统核心文件不被破坏。

### 核心能力

- 🎙️ **语音识别（STT）**：Faster-Whisper / SenseVoice-Small，支持热词增强（世界杯、奥运会、NBA 等专有名词）
- 🧠 **大语言模型（LLM）**：DeepSeek API，Function Calling 工具链（天气、新闻、HackerNews、应用启动、网页搜索、系统时间）
- 🔊 **语音合成（TTS）**：GPT-SoVITS / So-VITS-SVC，J.a.r.v.i.s 音色，流式合成低延迟
- 🌌 **三维粒子可视化**：Three.js WebGL，4 状态机（Standby / Listening / Processing / Speaking），60fps 渲染，对象池复用
- 🛡️ **安全拦截器**：AOP 拦截 `os.remove` / `shutil.rmtree` 等危险操作，保护系统目录
- 📊 **显存管理**：VRAM 监控守护线程，OOM 降级策略，强制量化

## 🎬 演示效果

> 📌 粒子可视化四状态：
>
> | 状态 | 名称 | 视觉效果 |
> |---|---|---|
> | 0 | Standby | 低频呼吸球面（5fps 节能） |
> | 1 | Listening | 麦克风 RMS 驱动的正弦波径向运动 |
> | 2 | Processing | 3D 卫星轨道（含倾角、透视投影、`lighter` 合成、运动模糊） |
> | 3 | Speaking | TTS 音频驱动的正弦波径向运动 |

## 🛠️ 技术栈

| 层级 | 技术 | 说明 |
|---|---|---|
| 前端框架 | Vue 3 + TypeScript | 严格模式，状态机驱动 |
| 桌面端 | Tauri v1 | 500×500 透明悬浮窗 |
| 渲染引擎 | Three.js (WebGL) | Points + ShaderMaterial + AdditiveBlending |
| 后端 | Python 3.10+ | WebSocket Server，绑定 0.0.0.0 |
| STT | Faster-Whisper / SenseVoice-Small | int8/float16 量化，cuda:0 |
| LLM | DeepSeek API | Function Calling 工具链 |
| TTS | GPT-SoVITS / So-VITS-SVC | J.a.r.v.i.s 音色，流式合成 |
| 向量数据库 | ChromaDB | 长期记忆存储 |
| 通信 | WebSocket | 全双工实时通信，支持局域网穿透 |

## 📦 项目结构

```
Jarvis_Assistant/
├── backend/                              # Python 后端
│   ├── main.py                           # WebSocket 服务入口 + VRAM 监控守护线程
│   ├── ai_engine.py                      # LLM 引擎与 Function Calling 工具链
│   ├── security_interceptor.py           # AOP 安全拦截器（文件操作白名单）
│   ├── app_web_urls.json                 # 应用启动配置
│   ├── requirements.txt                  # Python 依赖
│   ├── .env.example                      # 环境变量模板
│   └── test_*.py / cli_test.py           # 测试脚本
│
├── frontend/                             # Vue 3 + Tauri 前端
│   ├── src/
│   │   ├── engine/
│   │   │   └── ParticleEngine.ts         # Three.js 粒子渲染引擎（核心）
│   │   ├── components/
│   │   │   ├── AIVisualCore.vue          # AI 可视化核心组件
│   │   │   └── JarvisOrb.vue             # 悬浮球容器
│   │   ├── audio/
│   │   │   └── AudioPipeline.ts          # 音频采集与播放
│   │   ├── ws/
│   │   │   └── WSClient.ts               # WebSocket 客户端
│   │   ├── state/
│   │   │   └── StateMachine.ts           # 4 状态机
│   │   ├── mock/
│   │   │   └── MockWSController.ts       # 离线 Mock 控制器
│   │   ├── App.vue
│   │   ├── main.ts
│   │   └── types.ts
│   ├── src-tauri/                        # Tauri 配置（Cargo.toml / tauri.conf.json）
│   ├── .env.example                      # 前端环境变量模板
│   ├── vite.config.ts
│   └── package.json
│
├── .github/                              # GitHub 社区文件
│   ├── ISSUE_TEMPLATE/                   # Issue 模板（Bug / Feature）
│   ├── PULL_REQUEST_TEMPLATE.md          # PR 模板（含红线自检）
│   └── ISSUE_TEMPLATE/config.yml         # Issue 引导配置
│
├── PC桌面端智能悬浮窗助手产品需求与技术架构说明书（PRD & Architecture V1.0）.md
├── communication_protocol.md             # WebSocket 通信协议
├── Particle_Visual_Component.md          # 粒子视觉组件设计 v1
├── Particle_Visual_Component_v2.md       # 粒子视觉组件设计 v2
├── Vibe_Coding_Workflow_and_Defense_Specification.md  # 编码工作流与防御规范
├── 前端开发踩坑与经验总结.md              # Three.js 粒子渲染踩坑记录
├── 完整流程.txt                          # 原始 Three.js 粒子设计基准
├── CONTRIBUTING.md                       # 贡献指南
├── LICENSE                               # MIT License
└── README.md
```

## 🚀 快速开始

### 前置条件

| 依赖 | 版本 | 说明 |
|---|---|---|
| Node.js | 18+ | 前端构建 |
| Python | 3.10+ | 后端运行 |
| Rust toolchain | stable | Tauri v1 桌面端 |
| NVIDIA GPU | RTX 3060 6GB+ | CUDA 加速 |
| GPT-SoVITS | - | 独立运行的 TTS API 服务，默认端口 9880 |

### 1️⃣ 克隆仓库

```bash
git clone https://github.com/WorthChecking/jarvis-assistant.git
cd jarvis-assistant
```

### 2️⃣ 后端配置

```bash
cd backend
python -m venv venv
venv\Scripts\activate              # Windows
# source venv/bin/activate         # Linux/macOS

pip install -r requirements.txt

# 配置环境变量
cp .env.example .env               # 填入你的 API Key 等配置

# 启动 GPT-SoVITS API 服务（独立项目，端口 9880）

# 启动后端
python main.py
```

### 3️⃣ 前端配置

```bash
cd ../frontend
npm install

# 配置前端环境变量
cp .env.example .env               # 默认连接本地 ws://127.0.0.1:8765/ws

# 开发模式
npm run tauri dev
```

### 4️⃣ 配置说明

**后端 `.env`**（参考 [backend/.env.example](backend/.env.example)）：

```env
JARVIS_LLM_API_KEY=sk-your-deepseek-key
JARVIS_LLM_API_BASE=https://api.deepseek.com
JARVIS_LLM_MODEL=deepseek-chat
JARVIS_TTS_API_BASE=http://127.0.0.1:9880
JARVIS_WS_HOST=0.0.0.0
JARVIS_WS_PORT=8765
JARVIS_STT_VRAM_LIMIT_MB=3072
JARVIS_TTS_VRAM_LIMIT_MB=2048
```

**前端 `.env`**（参考 [frontend/.env.example](frontend/.env.example)）：

```env
# 本地开发
VITE_WS_URL=ws://127.0.0.1:8765/ws
# 虚拟机/局域网穿透：VITE_WS_URL=ws://<VM-LAN-IP>:8765/ws
```

## 🛡️ 安全红线

本项目严格遵守三条绝对红线（详见 PRD）：

| 红线 | 内容 |
|---|---|
| **资源红线** | RTX 3060 6GB 显存上限；AI 模型强制 int8/float16 量化；`device="cuda:0"` 防显存泄漏；VRAM 守护线程 |
| **安全红线** | 严禁修改/删除系统核心文件（`C:\Windows`、`C:\Program Files`）；文件操作经 AOP 拦截器白名单；危险调用（`os.remove` / `shutil.rmtree` / `subprocess.Popen` 等）默认拦截 |
| **渲染红线** | 高频状态渲染使用 Canvas/WebGL；**禁止** Vue DOM 双向绑定或纯 CSS 动画；粒子复用对象池 |

## 📚 文档导航

| 文档 | 说明 |
|---|---|
| [PRD & 技术架构说明书 V1.0](PC桌面端智能悬浮窗助手产品需求与技术架构说明书（PRD%20%26%20Architecture%20V1.0）.md) | **项目最高准则**，所有技术选型与功能边界以此为准 |
| [通信协议](communication_protocol.md) | WebSocket 前后端通信协议规范 |
| [粒子视觉组件 v2](Particle_Visual_Component_v2.md) | Three.js 粒子视觉设计文档 |
| [Vibe 编码工作流与防御规范](Vibe_Coding_Workflow_and_Defense_Specification.md) | 编码流程与防御性编程规范 |
| [前端开发踩坑与经验总结](前端开发踩坑与经验总结.md) | Three.js 粒子渲染踩坑全记录（15 条经验） |
| [贡献指南](CONTRIBUTING.md) | 开发环境、代码规范、提交规范、PR 流程 |

## 🗺️ Roadmap

- [x] 4 状态机粒子可视化（Standby / Listening / Processing / Speaking）
- [x] STT + LLM + TTS 全链路打通
- [x] Function Calling 工具链（天气/新闻/HackerNews/应用启动）
- [x] AOP 安全拦截器
- [x] VRAM 监控与 OOM 降级
- [ ] 长期记忆（ChromaDB 向量检索）
- [ ] 多轮对话上下文管理
- [ ] 自定义唤醒词
- [ ] 插件系统
- [ ] 跨平台支持（macOS / Linux）

## 🤝 贡献

欢迎贡献代码、报告问题或提出建议！请先阅读 [贡献指南](CONTRIBUTING.md)。

- 🐛 报告 Bug：[提交 Issue](https://github.com/WorthChecking/jarvis-assistant/issues/new?template=bug_report.md)
- 💡 功能建议：[提交 Issue](https://github.com/WorthChecking/jarvis-assistant/issues/new?template=feature_request.md)
- 🔧 贡献代码：Fork → 特性分支 → Pull Request

## 📄 License

本项目基于 [MIT License](LICENSE) 开源，版权所有 © 2026 WorthChecking。

## ⭐ Star History

如果这个项目对你有帮助，欢迎 Star 支持！

<div align="center">

**[⬆ 回到顶部](#jarvis)**

</div>
