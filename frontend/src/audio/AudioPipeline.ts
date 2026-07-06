/**
 * 音频管线 — 麦克风采集 + TTS 播放
 * 麦克风：16kHz 单声道 PCM int16 → Base64 分块上行
 * TTS 播放：接收 PCM int16 Base64 分块 → AudioContext 播放
 */

export class AudioPipeline {
  private wsSend: ((type: string, data: unknown) => void) | null = null
  private audioCtx: AudioContext | null = null
  private micStream: MediaStream | null = null
  private micProcessor: ScriptProcessorNode | null = null
  private ttsQueue: Int16Array[] = []
  private ttsPlaying: boolean = false
  private ttsSampleRate: number = 32000  // GPT-SoVITS 默认采样率
  private chunkIndex: number = 0
  private currentSource: AudioBufferSourceNode | null = null
  private nextPlayTime: number = 0  // 精确调度下一 chunk 的播放时间
  private readonly CROSSFADE_SAMPLES = 64  // 交叉淡入淡出样本数
  private readonly MIC_TARGET_RATE = 16000  // 麦克风上行目标采样率

  /** 绑定 WebSocket 发送函数 */
  bindSend(fn: (type: string, data: unknown) => void): void {
    this.wsSend = fn
  }

  /** 初始化 AudioContext（需在用户交互后调用） */
  async initAudioContext(): Promise<void> {
    if (!this.audioCtx) {
      // 使用浏览器默认采样率（通常 48000Hz），createBuffer 时指定实际 PCM 采样率即可
      this.audioCtx = new AudioContext()
    }
    if (this.audioCtx.state === 'suspended') {
      await this.audioCtx.resume()
    }
  }

  // ---- 麦克风采集 ----

  async startRecording(): Promise<void> {
    await this.initAudioContext()
    this.chunkIndex = 0

    try {
      this.micStream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })
    } catch (e) {
      console.error('[Audio] 麦克风获取失败:', e)
      return
    }

    const source = this.audioCtx!.createMediaStreamSource(this.micStream)
    // ScriptProcessorNode 缓冲区 4096 样本
    this.micProcessor = this.audioCtx!.createScriptProcessor(4096, 1, 1)

    this.micProcessor.onaudioprocess = (ev) => {
      const float32 = ev.inputBuffer.getChannelData(0)
      // 降采样到 16kHz（AudioContext 可能是 48kHz，ScriptProcessorNode 输出跟随 AudioContext 采样率）
      const resampled = downsampleFloat32(float32, this.audioCtx!.sampleRate, this.MIC_TARGET_RATE)
      const int16 = float32ToInt16(resampled)

      const pcmBase64 = arrayBufferToBase64(int16.buffer as ArrayBuffer)

      if (this.wsSend) {
        this.wsSend('audio_chunk', {
          sample_rate: this.MIC_TARGET_RATE,
          channels: 1,
          encoding: 'pcm_int16',
          chunk_index: this.chunkIndex++,
          audio_base64: pcmBase64,
        })
      }
    }

    source.connect(this.micProcessor)
    this.micProcessor.connect(this.audioCtx!.destination)
    console.log('[Audio] 麦克风录音已启动')
  }

  stopRecording(): void {
    if (this.micProcessor) {
      this.micProcessor.disconnect()
      this.micProcessor = null
    }
    if (this.micStream) {
      this.micStream.getTracks().forEach(t => t.stop())
      this.micStream = null
    }
    if (this.wsSend && this.chunkIndex > 0) {
      this.wsSend('audio_end', { total_chunks: this.chunkIndex, reason: 'user_stop' })
    }
    this.chunkIndex = 0
    console.log('[Audio] 麦克风录音已停止')
  }

  // ---- 唤醒词检测（State 0 低功耗持续监听）----

  private wakeWordActive = false
  private wakeWordStream: MediaStream | null = null
  private wakeWordProcessor: ScriptProcessorNode | null = null

  /**
   * 启动唤醒词检测：前端纯 CPU VAD 预过滤 + 音频录制
   * 检测到语音段后发送 wake_word_audio 消息到后端做 STT 关键词匹配
   */
  async startWakeWordDetection(): Promise<void> {
    if (this.wakeWordActive) return
    await this.initAudioContext()

    try {
      this.wakeWordStream = await navigator.mediaDevices.getUserMedia({
        audio: { sampleRate: 16000, channelCount: 1, echoCancellation: true, noiseSuppression: true },
      })
    } catch (e) {
      console.error('[Audio] 唤醒词检测：麦克风获取失败:', e)
      return
    }

    this.wakeWordActive = true
    const source = this.audioCtx!.createMediaStreamSource(this.wakeWordStream)
    this.wakeWordProcessor = this.audioCtx!.createScriptProcessor(4096, 1, 1)

    // VAD 参数
    const VAD_THRESHOLD = 0.015
    const SPEECH_START_MS = 200
    const SILENCE_END_MS = 800
    const PRE_BUFFER_CHUNKS = 4

    // VAD 状态
    let isSpeaking = false
    let speechStartTime = 0
    let lastVoiceTime = 0
    let preBuffer: Int16Array[] = []
    let wakeWordBuffer: Int16Array[] = []
    let totalSamples = 0

    this.wakeWordProcessor.onaudioprocess = (ev) => {
      if (!this.wakeWordActive) return

      const rawFloat32 = ev.inputBuffer.getChannelData(0)
      // 降采样到 16kHz
      const float32 = downsampleFloat32(rawFloat32, this.audioCtx!.sampleRate, this.MIC_TARGET_RATE)

      // 计算 RMS
      let sum = 0
      for (let i = 0; i < float32.length; i++) {
        sum += float32[i] * float32[i]
      }
      const rms = Math.sqrt(sum / float32.length)

      const int16 = float32ToInt16(float32)

      const now = performance.now()

      if (!isSpeaking) {
        // 待机：维护预缓冲区（保留最近 ~1 秒音频）
        preBuffer.push(int16)
        if (preBuffer.length > PRE_BUFFER_CHUNKS) {
          preBuffer.shift()
        }

        // 检测语音开始
        if (rms > VAD_THRESHOLD) {
          if (speechStartTime === 0) {
            speechStartTime = now
          } else if (now - speechStartTime > SPEECH_START_MS) {
            isSpeaking = true
            lastVoiceTime = now
            wakeWordBuffer = [...preBuffer]
            totalSamples = wakeWordBuffer.reduce((s, a) => s + a.length, 0)
          }
        } else {
          speechStartTime = 0
        }
      } else {
        // 录制中
        wakeWordBuffer.push(int16)
        totalSamples += int16.length

        if (rms > VAD_THRESHOLD) {
          lastVoiceTime = now
        } else if (now - lastVoiceTime > SILENCE_END_MS) {
          // 语音结束，发送音频到后端
          if (totalSamples > 1600 && this.wsSend) {
            // 合并所有 buffer
            const merged = new Int16Array(totalSamples)
            let offset = 0
            for (const buf of wakeWordBuffer) {
              merged.set(buf, offset)
              offset += buf.length
            }
            this.wsSend('wake_word_audio', {
              sample_rate: this.MIC_TARGET_RATE,
              channels: 1,
              encoding: 'pcm_int16',
              audio_base64: arrayBufferToBase64(merged.buffer),
              total_samples: totalSamples,
            })
            console.log('[Audio] 唤醒词音频已发送 | 时长: %.2fs', totalSamples / this.MIC_TARGET_RATE)
          }

          // 重置状态
          isSpeaking = false
          speechStartTime = 0
          wakeWordBuffer = []
          totalSamples = 0
          preBuffer = []
        }
      }
    }

    source.connect(this.wakeWordProcessor)
    this.wakeWordProcessor.connect(this.audioCtx!.destination)
    console.log('[Audio] 唤醒词检测已启动')
  }

  stopWakeWordDetection(): void {
    this.wakeWordActive = false
    if (this.wakeWordProcessor) {
      this.wakeWordProcessor.disconnect()
      this.wakeWordProcessor = null
    }
    if (this.wakeWordStream) {
      this.wakeWordStream.getTracks().forEach(t => t.stop())
      this.wakeWordStream = null
    }
    console.log('[Audio] 唤醒词检测已停止')
  }

  // ---- TTS 播放 ----

  /** 收集 MP3 分块（edge-tts 整体发送） */
  private mp3Buffer: string[] = []
  private mp3SampleRate: number = 24000

  feedTTSChunk(audioBase64: string, sampleRate: number, isFinal: boolean, encoding?: string): void {
    if (encoding === 'mp3') {
      // MP3 模式：收集完整数据后一次性解码
      this.mp3SampleRate = sampleRate
      this.mp3Buffer.push(audioBase64)
      if (isFinal && this.mp3Buffer.length > 0) {
        this.playMP3(this.mp3Buffer.join(''))
        this.mp3Buffer = []
      }
      return
    }

    // PCM int16 模式（原有逻辑）
    this.ttsSampleRate = sampleRate
    const pcmBytes = base64ToArrayBuffer(audioBase64)
    const int16 = new Int16Array(pcmBytes)
    this.ttsQueue.push(int16)

    if (!this.ttsPlaying) {
      this.playNextChunk()
    }
  }

  private async playMP3(base64Data: string): Promise<void> {
    await this.initAudioContext()
    try {
      const binary = atob(base64Data)
      const bytes = new Uint8Array(binary.length)
      for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i)
      const audioBuffer = await this.audioCtx!.decodeAudioData(bytes.buffer as ArrayBuffer)
      const source = this.audioCtx!.createBufferSource()
      source.buffer = audioBuffer
      source.connect(this.audioCtx!.destination)
      source.onended = () => { /* MP3 播放完毕 */ }
      source.start(0)
      console.log('[Audio] MP3 TTS 播放 | 时长: %.1fs', audioBuffer.duration)
    } catch (e) {
      console.error('[Audio] MP3 解码失败:', e)
    }
  }

  private async playNextChunk(): Promise<void> {
    if (this.ttsQueue.length === 0) {
      this.ttsPlaying = false
      this.currentSource = null
      return
    }

    this.ttsPlaying = true
    await this.initAudioContext()

    const int16 = this.ttsQueue.shift()!
    const float32 = new Float32Array(int16.length)
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / (int16[i] < 0 ? 0x8000 : 0x7FFF)
    }

    // 应用 crossfade：块首淡入 + 块尾淡出，消除块间阶跃爆音
    const fadeLen = Math.min(this.CROSSFADE_SAMPLES, Math.floor(float32.length / 2))
    for (let i = 0; i < fadeLen; i++) {
      const gain = i / fadeLen
      float32[i] *= gain            // 块首淡入
      float32[float32.length - 1 - i] *= gain  // 块尾淡出
    }

    const audioBuffer = this.audioCtx!.createBuffer(1, float32.length, this.ttsSampleRate)
    audioBuffer.copyToChannel(float32, 0)

    const source = this.audioCtx!.createBufferSource()
    source.buffer = audioBuffer
    source.connect(this.audioCtx!.destination)

    // 精确时间调度：无间隙衔接
    const now = this.audioCtx!.currentTime
    if (this.nextPlayTime <= now) {
      this.nextPlayTime = now + 0.02  // 20ms 初始缓冲
    }

    source.start(this.nextPlayTime)
    this.nextPlayTime += audioBuffer.duration

    this.currentSource = source
    source.onended = () => this.playNextChunk()
  }

  stopPlayback(): void {
    this.ttsQueue = []
    this.ttsPlaying = false
    this.nextPlayTime = 0
    if (this.currentSource) {
      try { this.currentSource.stop() } catch {}
      this.currentSource = null
    }
  }

  destroy(): void {
    this.stopWakeWordDetection()
    this.stopRecording()
    this.stopPlayback()
    if (this.audioCtx) {
      this.audioCtx.close()
      this.audioCtx = null
    }
  }
}


// ---- 工具函数 ----

function arrayBufferToBase64(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  let binary = ''
  for (let i = 0; i < bytes.byteLength; i++) {
    binary += String.fromCharCode(bytes[i])
  }
  return btoa(binary)
}

function base64ToArrayBuffer(base64: string): ArrayBuffer {
  const binary = atob(base64)
  const bytes = new Uint8Array(binary.length)
  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i)
  }
  return bytes.buffer
}

/** 线性插值降采样：将 float32 音频从 srcRate 降到 dstRate */
function downsampleFloat32(input: Float32Array, srcRate: number, dstRate: number): Float32Array {
  if (srcRate === dstRate) return input
  const ratio = srcRate / dstRate
  const outputLen = Math.round(input.length / ratio)
  const output = new Float32Array(outputLen)
  for (let i = 0; i < outputLen; i++) {
    const srcIdx = i * ratio
    const idx0 = Math.floor(srcIdx)
    const idx1 = Math.min(idx0 + 1, input.length - 1)
    const frac = srcIdx - idx0
    output[i] = input[idx0] * (1 - frac) + input[idx1] * frac
  }
  return output
}

/** float32 → int16 PCM */
function float32ToInt16(input: Float32Array): Int16Array {
  const int16 = new Int16Array(input.length)
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]))
    int16[i] = s < 0 ? s * 0x8000 : s * 0x7FFF
  }
  return int16
}
