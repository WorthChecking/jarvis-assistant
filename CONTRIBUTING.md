# 贡献指南

感谢你对 JARVIS 项目的关注！本文档说明了参与贡献的流程与规范。

## 📋 项目红线（必读）

在贡献代码前，请务必遵守以下三条绝对红线（详见 [PRD & 技术架构说明书](PC桌面端智能悬浮窗助手产品需求与技术架构说明书（PRD%20%26%20Architecture%20V1.0）.md)）：

1. **资源红线**：本机显存上限为 RTX 3060 6GB。所有 Faster-Whisper 与 TTS 加载代码必须显式指定量化参数（int8/float16）与严格的显存限制/释放策略。AI 引擎必须使用 `device="cuda:0"`。
2. **安全红线**：严禁生成任何可能修改/删除系统核心文件或格式化磁盘的脚本。系统操作必须遵照 PRD 操作白名单。涉及文件删除或修改标准库调用时，必须通过 [security_interceptor.py](backend/security_interceptor.py) 的 AOP 拦截器。
3. **渲染红线**：前端状态流转与波形/粒子 UI 必须使用 HTML5 Canvas API 或 WebGL（Three.js）底层绘制，**严禁**使用 Vue DOM 双向绑定或纯 CSS 动画进行高频状态渲染。

## 🛠️ 开发环境搭建

### 前置依赖

- Node.js 18+
- Python 3.10+
- Rust toolchain（Tauri v1）
- NVIDIA GPU（推荐 RTX 3060 6GB+，需支持 CUDA）
- GPT-SoVITS API（独立运行，默认端口 9880）

### 后端

```bash
cd backend
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env           # 填入你的 API Key
python main.py
```

### 前端

```bash
cd frontend
npm install
npm run tauri dev
```

## 📐 代码规范

### Python（后端）

- 使用 4 空格缩进
- 类型注解：公开函数必须标注参数与返回类型
- 显存相关代码必须包含 `torch.cuda.empty_cache()` 释放策略
- 涉及文件系统操作的代码必须经过 `security_interceptor` 装饰

### TypeScript（前端）

- 严格模式 `strict: true`（见 [tsconfig.json](frontend/tsconfig.json)）
- 状态机仅允许 4 个状态：`0=Standby / 1=Listening / 2=Processing / 3=Speaking`，int 类型与后端对齐
- 粒子渲染必须复用对象池，**禁止**在渲染循环中动态创建对象
- State 0 (Standby) 必须暂停 `requestAnimationFrame` 或降频至 5fps

### 状态机约定

| 状态码 | 名称 | 渲染要求 |
|---|---|---|
| 0 | Standby | 暂停 rAF 或降频 5fps |
| 1 | Listening | 60fps，正弦波径向运动 |
| 2 | Processing | 60fps，3D 卫星轨道（含倾角/透视投影/`lighter` 合成/运动模糊） |
| 3 | Speaking | 60fps，正弦波径向运动 |

## 📝 提交规范

使用 [Conventional Commits](https://www.conventionalcommits.org/) 规范：

```
<type>(<scope>): <subject>

<body>
```

### Type 列表

| Type | 说明 |
|---|---|
| `feat` | 新功能 |
| `fix` | Bug 修复 |
| `refactor` | 重构（不改变行为） |
| `perf` | 性能优化 |
| `docs` | 文档变更 |
| `style` | 代码格式（不影响逻辑） |
| `test` | 测试相关 |
| `chore` | 构建/工具/依赖变更 |

### Scope 建议

- `backend` — Python 后端
- `frontend` — Vue/Tauri 前端
- `particle` — Three.js 粒子引擎
- `stt` / `tts` / `llm` — AI 模块
- `security` — 安全拦截器
- `docs` — 文档

### 示例

```
feat(particle): State 2 卫星轨道增加倾角参数

refactor(security): 抽取白名单校验为独立函数

fix(tts): 修复 So-VITS-SVC 流式合成的截断问题
```

## 🔄 PR 流程

1. **Fork** 本仓库并创建特性分支：`git checkout -b feat/your-feature`
2. **开发**：遵循上述代码规范，确保本地通过基本验证
3. **提交**：按提交规范编写 commit message
4. **PR**：向 `main` 分支发起 Pull Request，填写 PR 模板
5. **Review**：等待代码审查，根据反馈调整
6. **Merge**：通过审查后合并

### PR 检查清单

- [ ] 不违反三条红线（资源/安全/渲染）
- [ ] 未引入 PRD 外的新框架
- [ ] 涉及 AI 模型的代码显式指定量化参数
- [ ] 涉及文件操作的代码经过安全拦截器
- [ ] 涉及高频渲染的代码使用 Canvas/WebGL
- [ ] commit message 符合规范
- [ ] 无硬编码密钥/凭据

## 🐛 报告问题

- Bug 请使用 [Bug Report 模板](.github/ISSUE_TEMPLATE/bug_report.md)
- 新功能建议请使用 [Feature Request 模板](.github/ISSUE_TEMPLATE/feature_request.md)
- 安全漏洞请**不要**在公开 Issue 中讨论，直接私信维护者

## 📧 联系方式

- 维护者：[@WorthChecking](https://github.com/WorthChecking)
- Issue：[https://github.com/WorthChecking/jarvis-assistant/issues](https://github.com/WorthChecking/jarvis-assistant/issues)

---

感谢你的贡献！🤖
