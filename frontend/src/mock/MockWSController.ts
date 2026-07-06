/**
 * MockWebSocketController — 脱离后端的本地调试控制器
 * 键盘快捷键：
 *   F1 → State 0 待机
 *   F2 → State 1 聆听（自动注入随机音量）
 *   F3 → State 2 处理中
 *   F4 → State 3 播报（自动注入随机音量）
 *   F5 → 触发警戒色脉冲
 *   F6 → 模拟 OOM 显存溢出降级
 *   F7 → 模拟 OOM 恢复
 *   F12 → 停止所有模拟
 */

import { UIState } from '../types'
import { StateMachine } from '../state/StateMachine'

export class MockWSController {
  private sm: StateMachine
  private volumeSimTimer: ReturnType<typeof setInterval> | null = null
  private bound: boolean = false

  constructor(sm: StateMachine) {
    this.sm = sm
  }

  bind(): void {
    if (this.bound) return
    this.bound = true
    window.addEventListener('keydown', this.onKey)
    console.log('[MockWS] 调试模式已激活 — F1~F7 切换状态, F12 停止')
  }

  unbind(): void {
    if (!this.bound) return
    this.bound = false
    window.removeEventListener('keydown', this.onKey)
    this.stopVolumeSim()
  }

  private onKey = (ev: KeyboardEvent): void => {
    switch (ev.key) {
      case 'F1':
        ev.preventDefault()
        this.stopVolumeSim()
        this.sm.transitionTo(UIState.Standby)
        console.log('[MockWS] → State 0 待机')
        break
      case 'F2':
        ev.preventDefault()
        this.sm.transitionTo(UIState.Listening)
        this.startVolumeSim(0.2, 0.6)
        console.log('[MockWS] → State 1 聆听（模拟麦克风音量）')
        break
      case 'F3':
        ev.preventDefault()
        this.stopVolumeSim()
        this.sm.transitionTo(UIState.Processing)
        console.log('[MockWS] → State 2 处理中（3D环绕）')
        break
      case 'F4':
        ev.preventDefault()
        this.sm.transitionTo(UIState.ActionSpeaking)
        this.startVolumeSim(0.3, 0.9)
        console.log('[MockWS] → State 3 播报（模拟TTS音量）')
        break
      case 'F5':
        ev.preventDefault()
        this.sm.triggerAlert()
        console.log('[MockWS] → 警戒色脉冲')
        break
      case 'F6':
        ev.preventDefault()
        console.log('[MockWS] → OOM 显存溢出模拟（降级）')
        this.sm.triggerAlert()
        // 模拟降级：强制切到 State 0 并闪烁警戒色
        this.sm.transitionTo(UIState.Standby)
        this.sm.triggerAlert()
        break
      case 'F7':
        ev.preventDefault()
        console.log('[MockWS] → OOM 恢复')
        this.sm.transitionTo(UIState.Standby)
        break
      case 'F12':
        ev.preventDefault()
        this.stopVolumeSim()
        this.sm.transitionTo(UIState.Standby)
        console.log('[MockWS] → 停止所有模拟')
        break
    }
  }

  private startVolumeSim(minV: number, maxV: number): void {
    this.stopVolumeSim()
    this.volumeSimTimer = setInterval(() => {
      const v = minV + Math.random() * (maxV - minV)
      const f = 200 + Math.random() * 600
      this.sm.setVolume(v, f)
    }, 16)
  }

  private stopVolumeSim(): void {
    if (this.volumeSimTimer) {
      clearInterval(this.volumeSimTimer)
      this.volumeSimTimer = null
      this.sm.setVolume(0, 0)
    }
  }
}
