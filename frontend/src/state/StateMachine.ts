/**
 * UI 状态机控制器
 * 严格在四种状态间流转，禁止中间态
 * 负责驱动 ParticleEngine + Tauri 窗口穿透切换
 */

import { UIState } from '../types'
import { ParticleEngine } from '../engine/ParticleEngine'

type StateChangeCallback = (state: UIState, prevState: UIState) => void

export class StateMachine {
  private current: UIState = UIState.Standby
  private engine: ParticleEngine
  private listeners: StateChangeCallback[] = []

  constructor(engine: ParticleEngine) {
    this.engine = engine
  }

  get state(): UIState {
    return this.current
  }

  onChange(fn: StateChangeCallback): void {
    this.listeners.push(fn)
  }

  /** 切换到指定状态，驱动粒子引擎 */
  async transitionTo(state: UIState): Promise<void> {
    if (this.current === state) return
    const prev = this.current
    this.current = state
    this.engine.setState(state)
    for (const fn of this.listeners) {
      fn(state, prev)
    }
  }

  setVolume(v: number, f: number = 0): void {
    this.engine.setVolume(v, f)
  }

  /** 触发警戒色脉冲 */
  triggerAlert(): void {
    this.engine.triggerAlert()
  }

  start(): void {
    this.engine.start()
  }

  stop(): void {
    this.engine.stop()
  }

  resize(): void {
    this.engine.resize()
  }

  dispose(): void {
    this.engine.dispose()
  }
}
