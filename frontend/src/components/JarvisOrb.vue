<template>
  <div class="jarvis-container" @contextmenu.prevent @mousedown="onMouseDown" @mouseup="onMouseUp"
       @mousemove="onMouseMove" @mouseleave="onMouseLeave">
    <!-- AIVisualCore：4000 粒子 Three.js WebGL 矩阵投影 + 流光拖尾（严格 0/1/2/3 四状态） -->
    <AIVisualCore :state="currentUIState" :volume-rms="currentVolumeRms" />
    <!-- 可见边框指示器 -->
    <div class="window-border" :class="{ dragging: isDragging, recording: isRecording }" />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { UIState, StateChangeData, VolumeRMSData, TTSChunkData, ActionExecuteData } from '../types'
import { WSClient } from '../ws/WSClient'
import { AudioPipeline } from '../audio/AudioPipeline'
import { MockWSController } from '../mock/MockWSController'
import AIVisualCore from './AIVisualCore.vue'
import { invoke } from '@tauri-apps/api/tauri'

// ---- AIVisualCore 通信契约数据 ----
const currentUIState = ref(0)         // 后端 int 状态码 0/1/2/3
const currentVolumeRms = ref(0)       // 音频响度 [0, 1]

let ws: WSClient | null = null
let audio: AudioPipeline | null = null
let mock: MockWSController | null = null

// 拖拽状态
const isDragging = ref(false)
let dragStartX = 0
let dragStartY = 0
let windowStartX = 0
let windowStartY = 0
let pressTimer: ReturnType<typeof setTimeout> | null = null
let isPressed = false
const LONG_PRESS_MS = 150

// 录音状态
const isRecording = ref(false)

onMounted(() => {
  audio = new AudioPipeline()

  ws = new WSClient()
  audio.bindSend((type, data) => ws!.send(type, data))

  // ---- 状态切换：直接映射后端 int 状态码到 AIVisualCore ----
  ws.on('state_change', (data: unknown) => {
    const d = data as StateChangeData
    // 严格校验：仅接受 0/1/2/3
    if (d.state >= 0 && d.state <= 3) {
      currentUIState.value = d.state
    }

    if (d.state === UIState.Standby) {
      audio!.stopRecording()
      isRecording.value = false
      audio!.startWakeWordDetection()
    } else if (d.state === UIState.Listening) {
      audio!.stopWakeWordDetection()
      audio!.startRecording()
      isRecording.value = true
    } else {
      audio!.stopRecording()
      audio!.stopWakeWordDetection()
      isRecording.value = false
    }
  })

  // ---- 音量数据：直接注入 AIVisualCore（[0,1] 范围）----
  ws.on('volume_rms', (data: unknown) => {
    const d = data as VolumeRMSData
    currentVolumeRms.value = d.v
  })

  ws.on('action_start', () => {
    console.log('[JarvisOrb] 收到 action_start，警戒反馈')
  })

  // 处理后端下发的系统操作指令（路由到 Tauri Rust 执行）
  ws.on('action_execute', async (data: unknown) => {
    const d = data as ActionExecuteData
    console.log('[ActionExec] 收到操作指令:', d.action_name, d.action_category, d.params)
    try {
      const result = await invoke<Record<string, unknown>>('exec_system_action', {
        action: d.action_name,
        params: d.params ?? {},
      })
      console.log('[ActionExec] 执行成功:', d.action_name, result)
      ws?.send('action_result', {
        action_id: d.action_id,
        success: true,
        result: result,
      })
    } catch (e) {
      console.error('[ActionExec] 执行失败:', d.action_name, e)
      ws?.send('action_result', {
        action_id: d.action_id,
        success: false,
        error: String(e),
      })
    }
  })

  ws.on('tts_chunk', (data: unknown) => {
    const d = data as TTSChunkData
    audio!.feedTTSChunk(d.audio_base64, d.sample_rate, d.is_final, (d as any).encoding)
  })

  ws.on('connection_established', () => {
    currentUIState.value = 0
    setTimeout(() => {
      if (currentUIState.value === 0) {
        audio!.startWakeWordDetection()
      }
    }, 1000)
  })

  ws.on('server_shutdown', () => {
    currentUIState.value = 0
  })

  ws.start()

  // Mock 控制器
  mock = new MockWSController({
    transitionTo: (state: UIState) => {
      if (state >= 0 && state <= 3) {
        currentUIState.value = state
      }
    },
    setVolume: (v: number) => {
      currentVolumeRms.value = v
    },
    triggerAlert: () => {
      console.log('[Mock] alert triggered')
    },
    start: () => {},
    stop: () => {},
    state: UIState.Standby,
  } as any)
  mock.bind()
})

onUnmounted(() => {
  ws?.stop()
  audio?.stopWakeWordDetection()
  audio?.destroy()
  mock?.unbind()
})

// ---- 点击切换录音（非拖拽场景）----
function onOrbitClick() {
  if (isDragging.value) return
  if (isRecording.value) {
    ws?.send('stop_recording', { reason: 'user_stop' })
  } else {
    ws?.send('start_recording', {})
  }
}

// ---- 长按拖拽逻辑 ----
async function getWinPos(): Promise<[number, number]> {
  try {
    return await invoke<[number, number]>('get_window_position')
  } catch { return [0, 0] }
}

async function setWinPos(x: number, y: number): Promise<void> {
  try {
    await invoke('set_window_position', { x: Math.round(x), y: Math.round(y) })
  } catch { /* ignore */ }
}

async function saveWinPos(x: number, y: number): Promise<void> {
  try {
    await invoke('save_window_position', { x, y })
  } catch { /* ignore */ }
}

function onMouseDown(ev: MouseEvent) {
  if (ev.button !== 0) return
  isPressed = true
  dragStartX = ev.screenX
  dragStartY = ev.screenY
  getWinPos().then(pos => {
    windowStartX = pos[0]
    windowStartY = pos[1]
  })
  pressTimer = setTimeout(() => {
    if (isPressed) {
      isDragging.value = true
      document.body.style.cursor = 'grabbing'
    }
  }, LONG_PRESS_MS)
}

async function onMouseMove(ev: MouseEvent) {
  if (!isDragging.value) return
  const dx = ev.screenX - dragStartX
  const dy = ev.screenY - dragStartY
  await setWinPos(windowStartX + dx, windowStartY + dy)
}

async function onMouseUp(ev: MouseEvent) {
  if (pressTimer) {
    clearTimeout(pressTimer)
    pressTimer = null
  }
  if (isDragging.value) {
    isDragging.value = false
    document.body.style.cursor = ''
    const pos = await getWinPos()
    await saveWinPos(pos[0], pos[1])
  } else if (isPressed) {
    onOrbitClick()
  }
  isPressed = false
}

function onMouseLeave() {
  if (pressTimer) {
    clearTimeout(pressTimer)
    pressTimer = null
  }
}
</script>

<style scoped>
.jarvis-container {
  width: 100vw;
  height: 100vh;
  overflow: hidden;
  background: #000;
  position: relative;
  user-select: none;
  display: flex;
  align-items: center;
  justify-content: center;
}

.window-border {
  position: absolute;
  inset: 0;
  border: 1px solid rgba(100, 180, 255, 0.25);
  border-radius: 8px;
  pointer-events: none;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
  box-shadow: inset 0 0 20px rgba(0, 0, 0, 0.3);
}

.window-border.dragging {
  border-color: rgba(255, 180, 80, 0.6);
  box-shadow: 0 4px 24px rgba(255, 140, 50, 0.25), inset 0 0 30px rgba(0, 0, 0, 0.3);
}

.window-border.recording {
  border-color: rgba(255, 80, 80, 0.7);
  box-shadow: 0 0 20px rgba(255, 60, 60, 0.3), inset 0 0 30px rgba(0, 0, 0, 0.3);
  animation: recording-pulse 1.5s ease-in-out infinite;
}

@keyframes recording-pulse {
  0%, 100% { border-color: rgba(255, 80, 80, 0.5); }
  50% { border-color: rgba(255, 80, 80, 0.9); }
}
</style>
