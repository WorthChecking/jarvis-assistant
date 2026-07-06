// ============================================================
// PC桌面端智能悬浮窗助手 — 前后端通信协议 TypeScript 类型定义
// 对应文档: communication_protocol.md V1.0
// ============================================================

// ---- 通用枚举 ----

/** UI 状态机枚举 */
export enum UIState {
  Standby = 0,
  Listening = 1,
  Processing = 2,
  ActionSpeaking = 3,
}

/** 操作白名单分类 */
export enum ActionCategory {
  DeviceControl = 'device_control',
  AppManagement = 'app_management',
  FileSearch = 'file_search',
  FileRead = 'file_read',
}

/** 错误严重级别 */
export enum ErrorSeverity {
  Warning = 'warning',
  Error = 'error',
  Critical = 'critical',
}

/** 进入待机原因 */
export type IdleReason = 'timeout' | 'user_cancel' | 'session_end' | 'startup';

/** 录音结束原因 */
export type AudioEndReason = 'vad_silence' | 'user_stop';

/** State 3 播报模式 */
export type ActionSpeakingMode = 'speaking' | 'action' | 'action_and_speaking';

/** 音频编码格式 */
export type AudioEncoding = 'pcm_int16';

// ---- 消息顶层结构 ----

/** 所有 WebSocket 消息的顶层包装 */
export interface WSMessage<T extends string, D = unknown> {
  type: T;
  data: D;
}

// ---- 心跳 ----

export interface PingData {
  timestamp: number;
}

export interface PongData {
  timestamp: number;
}

export type PingMessage = WSMessage<'ping', PingData>;
export type PongMessage = WSMessage<'pong', PongData>;

// ---- 连接生命周期 ----

export interface ConnectionEstablishedData {
  session_id: string;
  server_version: string;
}

export type ConnectionEstablishedMessage = WSMessage<'connection_established', ConnectionEstablishedData>;

export interface CloseData {
  reason: string;
}

export type CloseMessage = WSMessage<'close', CloseData>;

export interface ServerShutdownData {
  reason: string;
}

export type ServerShutdownMessage = WSMessage<'server_shutdown', ServerShutdownData>;

// ---- 状态切换 ----

/** State 0 — 待机 payload */
export interface StandbyPayload {
  idle_reason: IdleReason;
}

/** State 1 — 聆听 payload */
export interface ListeningPayload {
  wake_word: string;
  vad_enabled: boolean;
  vad_silence_threshold_ms: number;
}

/** State 2 — 处理中 payload */
export interface ProcessingPayload {
  stt_text: string;
  has_function_call: boolean;
}

/** State 3 — 播报与执行 payload */
export interface ActionSpeakingPayload {
  mode: ActionSpeakingMode;
  tts_text: string | null;
  action: ActionStartData | null;
}

/** 状态切换 data 结构 */
export interface StateChangeData {
  state: UIState;
  prev_state: UIState | null;
  timestamp: number;
  payload: StandbyPayload | ListeningPayload | ProcessingPayload | ActionSpeakingPayload;
}

export type StateChangeMessage = WSMessage<'state_change', StateChangeData>;

// ---- 麦克风音频流（前端 → 后端）----

export interface AudioChunkData {
  sample_rate: 16000;
  channels: 1;
  encoding: AudioEncoding;
  chunk_index: number;
  audio_base64: string;
}

export type AudioChunkMessage = WSMessage<'audio_chunk', AudioChunkData>;

export interface AudioEndData {
  total_chunks: number;
  reason: AudioEndReason;
}

export type AudioEndMessage = WSMessage<'audio_end', AudioEndData>;

// ---- TTS 音频流（后端 → 前端）----

export interface TTSChunkData {
  sample_rate: number;
  channels: number;
  encoding: AudioEncoding | 'mp3';
  chunk_index: number;
  audio_base64: string;
  is_final: boolean;
}

export type TTSChunkMessage = WSMessage<'tts_chunk', TTSChunkData>;

// ---- 实时响度流 ----

/** TTS 响度（后端 → 前端，60fps） */
export interface VolumeRMSData {
  /** Unix 毫秒时间戳 */
  t: number;
  /** 归一化 RMS 响度 [0.0, 1.0] */
  v: number;
  /** 当前主频率 Hz，无有效值时为 0.0 */
  f: number;
}

export type VolumeRMSMessage = WSMessage<'volume_rms', VolumeRMSData>;

/** 麦克风响度（前端 → 后端） */
export interface MicVolumeRMSData {
  t: number;
  v: number;
}

export type MicVolumeRMSMessage = WSMessage<'mic_volume_rms', MicVolumeRMSData>;

// ---- 系统操作触发器 ----

/** 操作开始通知（后端 → 前端） */
export interface ActionStartData {
  action_id: string;
  action_name: string;
  action_category: ActionCategory;
  params: Record<string, unknown>;
  timestamp: number;
}

export type ActionStartMessage = WSMessage<'action_start', ActionStartData>;

/** 前端执行指令（后端 → 前端，系统操作路由） */
export interface ActionExecuteData {
  action_id: string;
  action_name: string;
  action_category: string;
  params: Record<string, unknown>;
  timestamp: number;
}

export type ActionExecuteMessage = WSMessage<'action_execute', ActionExecuteData>;

export interface ActionEndData {
  action_id: string;
  success: boolean;
  result: Record<string, unknown> | null;
  error: string | null;
  timestamp: number;
}

export type ActionEndMessage = WSMessage<'action_end', ActionEndData>;

// ---- STT 流式结果 ----

export interface STTPartialData {
  text: string;
  is_final: boolean;
}

export type STTPartialMessage = WSMessage<'stt_partial', STTPartialData>;

// ---- 记忆检索 ----

export interface MemoryMatch {
  content: string;
  score: number;
  timestamp: number;
}

export interface MemoryRetrievedData {
  query: string;
  matches: MemoryMatch[];
}

export type MemoryRetrievedMessage = WSMessage<'memory_retrieved', MemoryRetrievedData>;

// ---- 错误与系统状态 ----

export interface ErrorData {
  code: string;
  message: string;
  severity: ErrorSeverity;
  recoverable: boolean;
}

export type ErrorMessage = WSMessage<'error', ErrorData>;

export interface SystemStatusData {
  vram_usage_percent: number;
  stt_loaded: boolean;
  tts_loaded: boolean;
  llm_connected: boolean;
  memory_db_active: boolean;
}

export type SystemStatusMessage = WSMessage<'system_status', SystemStatusData>;

// ---- 消息类型联合 ----

/** 后端 → 前端所有消息类型 */
export type ServerMessage =
  | PongMessage
  | ConnectionEstablishedMessage
  | ServerShutdownMessage
  | StateChangeMessage
  | TTSChunkMessage
  | VolumeRMSMessage
  | ActionStartMessage
  | ActionEndMessage
  | ActionExecuteMessage
  | STTPartialMessage
  | MemoryRetrievedMessage
  | ErrorMessage
  | SystemStatusMessage;

/** 前端 → 后端所有消息类型 */
export type ClientMessage =
  | PingMessage
  | CloseMessage
  | AudioChunkMessage
  | AudioEndMessage
  | MicVolumeRMSMessage;

/** 消息类型字符串字面量联合（后端 → 前端） */
export type ServerMessageType = ServerMessage['type'];

/** 消息类型字符串字面量联合（前端 → 后端） */
export type ClientMessageType = ClientMessage['type'];
