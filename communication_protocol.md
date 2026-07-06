# PC桌面端智能悬浮窗助手 — 前后端通信协议规范（V1.0）

## 1. 协议选型与总则

| 项目 | 说明 |
|------|------|
| 传输协议 | **WebSocket**（`ws://127.0.0.1:PORT/ws`） |
| 数据格式 | 所有上行/下行消息体均为 **JSON** |
| 字符编码 | UTF-8 |
| 设计原则 | 前端（Tauri + Vue 3）为被动消费端，后端（Python）为状态权威源；所有状态流转由后端驱动，前端仅上报用户交互事件 |

---

## 2. WebSocket 连接生命周期与心跳机制

### 2.1 连接建立

1. 前端启动后立即向 `ws://127.0.0.1:PORT/ws` 发起 WebSocket 握手。
2. 握手成功后，后端发送 `connection_established` 消息，前端进入 **State 0（待机）**。
3. 若握手失败，前端以指数退避策略重连（初始 1s，上限 30s）。

### 2.2 心跳保活

| 参数 | 值 |
|------|----|
| 心跳间隔 | **5 秒** |
| 超时判定 | 连续 **3 次** 未收到 PONG 响应（即 15 秒） |
| 超时动作 | 前端判定连接断开，触发重连流程 |

**心跳帧格式：**

```json
// 前端 → 后端（PING）
{
  "type": "ping",
  "timestamp": 1717660800000
}

// 后端 → 前端（PONG）
{
  "type": "pong",
  "timestamp": 1717660800000
}
```

### 2.3 断线重连

1. 前端检测到连接关闭或心跳超时后，立即将 UI 切回 **State 0（待机）** 并停止所有音频流渲染。
2. 按指数退避策略重新发起握手。
3. 重连成功后，后端重新发送 `connection_established`，前端恢复待机态。

### 2.4 连接关闭

- 前端窗口销毁前主动发送 `close` 消息，后端收到后释放对应会话资源。
- 后端服务关闭前向所有连接广播 `server_shutdown` 消息。

```json
{
  "type": "close",
  "reason": "user_exit"
}
```

```json
{
  "type": "server_shutdown",
  "reason": "service_stopping"
}
```

---

## 3. 状态切换指令

UI 严格在四种状态间流转，所有状态切换由后端主动推送，前端不得自行推断状态。

### 3.1 通用状态切换消息结构

```json
{
  "type": "state_change",
  "data": {
    "state": 0,
    "prev_state": 3,
    "timestamp": 1717660800000,
    "payload": {}
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `state` | `integer (0-3)` | 目标状态编号 |
| `prev_state` | `integer (0-3) \| null` | 前一状态，首次连接时为 `null` |
| `timestamp` | `integer` | Unix 毫秒时间戳 |
| `payload` | `object` | 状态附加数据，各状态不同 |

### 3.2 各状态 payload 定义

#### State 0 — 待机 (Standby)

```json
{
  "type": "state_change",
  "data": {
    "state": 0,
    "prev_state": 3,
    "timestamp": 1717660800000,
    "payload": {
      "idle_reason": "timeout"
    }
  }
}
```

| payload 字段 | 类型 | 说明 |
|-------------|------|------|
| `idle_reason` | `string` | 进入待机原因：`"timeout"` / `"user_cancel"` / `"session_end"` / `"startup"` |

#### State 1 — 聆听 (Listening)

```json
{
  "type": "state_change",
  "data": {
    "state": 1,
    "prev_state": 0,
    "timestamp": 1717660800000,
    "payload": {
      "wake_word": "你好助手",
      "vad_enabled": true,
      "vad_silence_threshold_ms": 1500
    }
  }
}
```

| payload 字段 | 类型 | 说明 |
|-------------|------|------|
| `wake_word` | `string` | 触发唤醒的唤醒词文本 |
| `vad_enabled` | `boolean` | 是否启用语音活动检测（VAD）自动截断 |
| `vad_silence_threshold_ms` | `integer` | VAD 静音判定阈值（毫秒），超时自动结束录音 |

#### State 2 — 处理中 (Processing)

```json
{
  "type": "state_change",
  "data": {
    "state": 2,
    "prev_state": 1,
    "timestamp": 1717660800000,
    "payload": {
      "stt_text": "帮我调大音量",
      "has_function_call": true
    }
  }
}
```

| payload 字段 | 类型 | 说明 |
|-------------|------|------|
| `stt_text` | `string` | STT 识别出的完整文本 |
| `has_function_call` | `boolean` | LLM 是否返回了 Function Calling 指令 |

#### State 3 — 播报与执行 (Action & Speaking)

```json
{
  "type": "state_change",
  "data": {
    "state": 3,
    "prev_state": 2,
    "timestamp": 1717660800000,
    "payload": {
      "mode": "speaking",
      "tts_text": "已为您将音量调大",
      "action": null
    }
  }
}
```

| payload 字段 | 类型 | 说明 |
|-------------|------|------|
| `mode` | `string` | `"speaking"`（纯语音播报）/ `"action"`（系统操作执行）/ `"action_and_speaking"`（操作+播报） |
| `tts_text` | `string \| null` | 播报文本，`mode` 为 `"action"` 时可为 `null` |
| `action` | `object \| null` | 操作详情，见第 5 节 |

---

## 4. 实时音频流与响度流

### 4.1 麦克风音频流（前端 → 后端）

前端在 **State 1（聆听）** 期间，将麦克风采集的 PCM 音频分块上行发送至后端。

```json
{
  "type": "audio_chunk",
  "data": {
    "sample_rate": 16000,
    "channels": 1,
    "encoding": "pcm_int16",
    "chunk_index": 42,
    "audio_base64": "<base64_encoded_pcm_data>"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `sample_rate` | `integer` | 采样率，固定 16000 |
| `channels` | `integer` | 声道数，固定 1（单声道） |
| `encoding` | `string` | 编码格式，固定 `"pcm_int16"` |
| `chunk_index` | `integer` | 分块序号，单调递增，用于后端排序 |
| `audio_base64` | `string` | Base64 编码的 PCM 原始音频数据 |

### 4.2 录音结束通知（前端 → 后端）

VAD 检测到静音超时或用户手动停止时，前端发送结束标记。

```json
{
  "type": "audio_end",
  "data": {
    "total_chunks": 87,
    "reason": "vad_silence"
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `total_chunks` | `integer` | 本次录音总块数 |
| `reason` | `string` | 结束原因：`"vad_silence"` / `"user_stop"` |

### 4.3 TTS 音频流（后端 → 前端）

后端在 **State 3（播报）** 期间，将 TTS 生成的 PCM 音频分块下行推送至前端播放。

```json
{
  "type": "tts_chunk",
  "data": {
    "sample_rate": 24000,
    "channels": 1,
    "encoding": "pcm_int16",
    "chunk_index": 5,
    "audio_base64": "<base64_encoded_pcm_data>",
    "is_final": false
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `sample_rate` | `integer` | TTS 采样率 |
| `channels` | `integer` | 声道数 |
| `encoding` | `string` | 编码格式 |
| `chunk_index` | `integer` | 分块序号 |
| `audio_base64` | `string` | Base64 编码的 PCM 音频数据 |
| `is_final` | `boolean` | 是否为最后一个分块 |

### 4.4 实时响度流（后端 → 前端）

后端以 **60fps** 频率向前端推送 Volume RMS 数据，驱动波形/粒子动画渲染。此为最高频消息，字段名极致精简以降低序列化开销。

```json
{
  "type": "volume_rms",
  "data": {
    "t": 1717660800123,
    "v": 0.72,
    "f": 440.0
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `t` | `integer` | Unix 毫秒时间戳 |
| `v` | `float` | 归一化 RMS 响度值，范围 `[0.0, 1.0]` |
| `f` | `float` | 当前音频主频率（Hz），用于驱动粒子色彩/形态变化；无有效频率时为 `0.0` |

**后端实现要求：**
- Python 端通过 `pyaudio` 或 `sounddevice` 回调获取 TTS 输出缓冲区数据。
- 每帧（约 16.67ms）计算一次 RMS：`rms = sqrt(mean(samples^2))`，归一化至 `[0, 1]`。
- 使用独立线程以 60fps 定时推送，避免阻塞主控事件循环。

### 4.5 麦克风实时响度流（前端 → 后端）

前端在 **State 1（聆听）** 期间，同步将麦克风实时响度上行发送，供后端 VAD 辅助判断。

```json
{
  "type": "mic_volume_rms",
  "data": {
    "t": 1717660800123,
    "v": 0.35
  }
}
```

---

## 5. 本地系统操作触发器（Function Calling 通知）

当 LLM 返回 Function Calling 指令时，后端需同时通知前端进入警戒色状态，并告知操作执行结果。

### 5.1 操作开始通知（后端 → 前端）

```json
{
  "type": "action_start",
  "data": {
    "action_id": "act_20240606_001",
    "action_name": "volume_up",
    "action_category": "device_control",
    "params": {
      "step": 10
    },
    "timestamp": 1717660800000
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | `string` | 操作唯一标识，用于关联开始与结束消息 |
| `action_name` | `string` | 操作名称，对应白名单池中的动作标识 |
| `action_category` | `string` | 操作分类：`"device_control"` / `"app_management"` / `"file_search"` / `"file_read"` |
| `params` | `object` | 操作参数，各动作不同 |
| `timestamp` | `integer` | Unix 毫秒时间戳 |

**前端行为：** 收到 `action_start` 后，UI 粒子闪烁警戒色（红色脉冲），持续至收到对应的 `action_end`。

### 5.2 操作结束通知（后端 → 前端）

```json
{
  "type": "action_end",
  "data": {
    "action_id": "act_20240606_001",
    "success": true,
    "result": {
      "current_volume": 80
    },
    "error": null,
    "timestamp": 1717660800500
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_id` | `string` | 与 `action_start` 中的 `action_id` 一一对应 |
| `success` | `boolean` | 操作是否执行成功 |
| `result` | `object \| null` | 操作返回数据 |
| `error` | `string \| null` | 失败时的错误信息 |
| `timestamp` | `integer` | Unix 毫秒时间戳 |

### 5.3 操作白名单分类枚举

| action_category | 包含的 action_name |
|----------------|-------------------|
| `device_control` | `volume_up`, `volume_down`, `volume_mute`, `media_play_pause`, `media_next`, `media_prev`, `window_minimize`, `window_maximize`, `system_lock` |
| `app_management` | `app_launch` |
| `file_search` | `file_search_everything` |
| `file_read` | `file_read_content` |

---

## 6. 记忆与对话辅助消息

### 6.1 记忆检索结果通知（后端 → 前端，可选展示）

```json
{
  "type": "memory_retrieved",
  "data": {
    "query": "帮我调大音量",
    "matches": [
      {
        "content": "用户习惯将音量保持在 60-80 之间",
        "score": 0.89,
        "timestamp": 1717600000000
      }
    ]
  }
}
```

### 6.2 STT 中间结果（后端 → 前端，流式识别）

```json
{
  "type": "stt_partial",
  "data": {
    "text": "帮我调大",
    "is_final": false
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | `string` | 当前识别文本 |
| `is_final` | `boolean` | `false` 为中间结果，`true` 为最终结果 |

---

## 7. 错误与系统消息

### 7.1 错误消息（后端 → 前端）

```json
{
  "type": "error",
  "data": {
    "code": "STT_MODEL_LOAD_FAILED",
    "message": "Faster-Whisper 模型加载失败，显存不足",
    "severity": "critical",
    "recoverable": false
  }
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | `string` | 错误码，大写下划线命名 |
| `message` | `string` | 人类可读的错误描述 |
| `severity` | `string` | `"warning"` / `"error"` / `"critical"` |
| `recoverable` | `boolean` | 是否可自动恢复 |

### 7.2 系统状态消息（后端 → 前端）

```json
{
  "type": "system_status",
  "data": {
    "vram_usage_percent": 78.5,
    "stt_loaded": true,
    "tts_loaded": true,
    "llm_connected": true,
    "memory_db_active": true
  }
}
```

---

## 8. 消息类型汇总表

| 方向 | type | 触发场景 |
|------|------|---------|
| 双向 | `ping` / `pong` | 心跳保活 |
| 双向 | `close` | 主动断开 |
| 后端→前端 | `connection_established` | 连接建立成功 |
| 后端→前端 | `server_shutdown` | 服务关闭 |
| 后端→前端 | `state_change` | UI 状态切换 |
| 前端→后端 | `audio_chunk` | 麦克风音频分块 |
| 前端→后端 | `audio_end` | 录音结束 |
| 前端→后端 | `mic_volume_rms` | 麦克风实时响度 |
| 后端→前端 | `tts_chunk` | TTS 音频分块 |
| 后端→前端 | `volume_rms` | TTS 实时响度（60fps） |
| 后端→前端 | `action_start` | 系统操作开始 |
| 后端→前端 | `action_end` | 系统操作结束 |
| 后端→前端 | `stt_partial` | STT 流式中间结果 |
| 后端→前端 | `memory_retrieved` | 记忆检索结果 |
| 后端→前端 | `error` | 错误通知 |
| 后端→前端 | `system_status` | 系统资源状态 |
