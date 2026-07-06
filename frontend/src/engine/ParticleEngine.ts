/**
 * 粒子渲染引擎 — Three.js WebGL + 3D 透视投影 + 自定义着色器 + 流光拖尾
 * 严格对齐 完整流程.txt (AI State Visualizer V3) 原始设计
 * 状态流转：State 0(Standby) → State 1(Listening) → State 2(Processing) → State 3(Speaking)
 */

import * as THREE from 'three'
import { UIState } from '../types'

const PARTICLE_COUNT = 4000
const ORBIT_RING_COUNT = 10

const COLOR_COOL = new THREE.Color(0x00ffff)
const COLOR_WARM = new THREE.Color(0xffa500)
const COLOR_ALERT = new THREE.Color(0xff3232)

interface ParticleDatum {
  index: number
  spherePhi: number
  sphereTheta: number
  baseRadius: number
  ringId: number
  orbitRadius: number
  tiltX: number
  tiltZ: number
  currentOrbitAngle: number
  orbitSpeed: number
  noiseOffset: number
}

export class ParticleEngine {
  private container: HTMLElement
  private renderer: THREE.WebGLRenderer
  private scene: THREE.Scene
  private fadeScene: THREE.Scene
  private camera: THREE.PerspectiveCamera
  private particleSystem: THREE.Points
  private geometry: THREE.BufferGeometry
  private shaderMaterial: THREE.ShaderMaterial
  private fadePlane: THREE.Mesh

  private positions: Float32Array
  private colors: Float32Array
  private sizes: Float32Array
  private particleData: ParticleDatum[]

  private currentState: UIState = UIState.Standby
  private volumeRMS: number = 0
  private targetVolumeRMS: number = 0
  private alertPulse: number = 0
  private targetHue: THREE.Color = COLOR_COOL.clone()

  private running: boolean = false
  private animFrameId: number = 0

  // 自管时间（抛弃 Three.js Clock，避免 getDelta/getElapsedTime 双调用问题）
  private lastFrameTime: number = 0
  private animTime: number = 0

  // 自转参数
  private rotationSpeedX: number = 0
  private rotationSpeedY: number = 0
  private rotationSpeedZ: number = 0
  private nextRotationChange: number = 0

  constructor(container: HTMLElement) {
    this.container = container
    this.particleData = []

    // Scene
    this.scene = new THREE.Scene()
    this.scene.fog = new THREE.FogExp2(0x000000, 0.012)

    // Camera
    const w = container.clientWidth || window.innerWidth
    const h = container.clientHeight || window.innerHeight
    this.camera = new THREE.PerspectiveCamera(45, w / h, 1, 1000)
    this.camera.position.z = 65

    // Renderer
    this.renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: false })
    this.renderer.setPixelRatio(window.devicePixelRatio)
    this.renderer.setSize(w, h)
    this.renderer.autoClearColor = false
    container.appendChild(this.renderer.domElement)

    // Fade scene (motion blur / 流光残影)
    // opacity=0.3：残影衰减时间常数 2.8 帧（vs 0.15 的 6.15 帧），
    // 5 帧后残影亮度 16.8%（vs 44%），显著减少 State 1/3 高速运动时的离散残影点
    const fadeMaterial = new THREE.MeshBasicMaterial({
      color: 0x000000,
      transparent: true,
      opacity: 0.3,
      depthTest: false,
    })
    this.fadePlane = new THREE.Mesh(new THREE.PlaneGeometry(1000, 1000), fadeMaterial)
    this.fadePlane.position.z = -100
    this.fadeScene = new THREE.Scene()
    this.fadeScene.add(this.fadePlane)

    // Particles
    this.positions = new Float32Array(PARTICLE_COUNT * 3)
    this.colors = new Float32Array(PARTICLE_COUNT * 3)
    this.sizes = new Float32Array(PARTICLE_COUNT)

    this.initParticleData()

    this.geometry = new THREE.BufferGeometry()
    this.geometry.setAttribute('position', new THREE.BufferAttribute(this.positions, 3))
    this.geometry.setAttribute('color', new THREE.BufferAttribute(this.colors, 3))
    this.geometry.setAttribute('size', new THREE.BufferAttribute(this.sizes, 1))

    this.shaderMaterial = new THREE.ShaderMaterial({
      uniforms: {
        time: { value: 0 },
        globalOpacity: { value: 1.0 },
      },
      vertexShader: `
        attribute float size;
        attribute vec3 color;
        varying vec3 vColor;
        uniform float time;
        void main() {
          vColor = color;
          vec4 mvPosition = modelViewMatrix * vec4(position, 1.0);
          gl_PointSize = size * (400.0 / -mvPosition.z);
          gl_Position = projectionMatrix * mvPosition;
        }
      `,
      fragmentShader: `
        varying vec3 vColor;
        uniform float globalOpacity;
        void main() {
          vec2 xy = gl_PointCoord.xy - vec2(0.5);
          float ll = length(xy);
          if (ll > 0.5) discard;
          float alpha = (1.0 - (ll * 2.0));
          alpha = pow(alpha, 2.0) * globalOpacity;
          gl_FragColor = vec4(vColor, alpha);
        }
      `,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
      transparent: true,
    })

    this.particleSystem = new THREE.Points(this.geometry, this.shaderMaterial)
    this.scene.add(this.particleSystem)
  }

  private initParticleData(): void {
    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const i3 = i * 3
      const phi = Math.acos(1 - 2 * (i + 0.5) / PARTICLE_COUNT)
      const theta = Math.PI * (1 + Math.sqrt(5)) * (i + 0.5)

      const orbitRingId = Math.floor(Math.random() * ORBIT_RING_COUNT)
      const tiltX = (orbitRingId * Math.PI) / 3 + (Math.random() * 0.4 - 0.2)
      const tiltZ = (orbitRingId * Math.PI) / 5 + (Math.random() * 0.4 - 0.2)
      const orbitRadius = 12 + (orbitRingId * 3.5) + Math.random() * 1.5
      const orbitAngle = Math.random() * Math.PI * 2

      this.particleData.push({
        index: i,
        spherePhi: phi,
        sphereTheta: theta,
        baseRadius: 5.0 + Math.random() * 3.0,
        ringId: orbitRingId,
        orbitRadius,
        tiltX,
        tiltZ,
        currentOrbitAngle: orbitAngle,
        orbitSpeed: 0.6 + (Math.random() * 0.4) + (orbitRingId * 0.08),
        noiseOffset: Math.random() * 100,
      })

      this.positions[i3] = 0
      this.positions[i3 + 1] = 0
      this.positions[i3 + 2] = 0
      this.colors[i3] = COLOR_COOL.r
      this.colors[i3 + 1] = COLOR_COOL.g
      this.colors[i3 + 2] = COLOR_COOL.b
      this.sizes[i] = 1.2
    }
  }

  resize(): void {
    const w = this.container.clientWidth || window.innerWidth
    const h = this.container.clientHeight || window.innerHeight
    this.camera.aspect = w / h
    this.camera.updateProjectionMatrix()
    this.renderer.setSize(w, h)
  }

  setState(state: UIState): void {
    if (this.currentState === state) return
    if (state < 0 || state > 3) return
    this.currentState = state
    if (state >= UIState.Processing) {
      this.targetHue = COLOR_WARM
    } else {
      this.targetHue = COLOR_COOL
    }
  }

  setVolume(v: number, f: number = 0): void {
    // 不直接赋值，由 animate 中 lerp 平滑，避免麦克风 RMS 高频波动导致粒子抖动残影
    this.targetVolumeRMS = v
  }

  triggerAlert(): void {
    this.alertPulse = 1.0
  }

  start(): void {
    if (this.running) return
    this.running = true
    this.lastFrameTime = 0
    this.animTime = 0
    this.animate()
  }

  stop(): void {
    this.running = false
    if (this.animFrameId) {
      cancelAnimationFrame(this.animFrameId)
      this.animFrameId = 0
    }
  }

  dispose(): void {
    this.stop()
    this.geometry.dispose()
    this.shaderMaterial.dispose()
    this.fadePlane.geometry.dispose()
    ;(this.fadePlane.material as THREE.Material).dispose()
    this.renderer.dispose()
    if (this.renderer.domElement.parentElement) {
      this.renderer.domElement.parentElement.removeChild(this.renderer.domElement)
    }
  }

  private rotatePoint3D(x: number, y: number, z: number, pitchX: number, yawZ: number): { x: number; y: number; z: number } {
    let tempY = y * Math.cos(pitchX) - z * Math.sin(pitchX)
    let tempZ = y * Math.sin(pitchX) + z * Math.cos(pitchX)
    let finalX = x * Math.cos(yawZ) - tempY * Math.sin(yawZ)
    let finalY = x * Math.sin(yawZ) + tempY * Math.cos(yawZ)
    return { x: finalX, y: finalY, z: tempZ }
  }

  private animate(): void {
    if (!this.running) return

    this.animFrameId = requestAnimationFrame(() => this.animate())

    const now = performance.now()

    // 首帧：只记录时间戳，不更新
    if (this.lastFrameTime === 0) {
      this.lastFrameTime = now
      return
    }

    const rawDt = (now - this.lastFrameTime) / 1000
    this.lastFrameTime = now

    // 拒绝异常帧（冷启动/切屏/后台恢复等）
    if (rawDt <= 0.001 || rawDt > 0.1) return

    const dt = rawDt
    this.animTime += dt
    const time = this.animTime

    this.shaderMaterial.uniforms.time.value = time

    const s = this.currentState

    // 音量平滑：k=10（时间常数 0.1s），消除麦克风 RMS 高频波动
    // 未平滑时 vol 突变 → maxExpansion 突变 → lerp 目标突变 → 粒子抖动 + 旧位置残影
    const volLerpK = 1 - Math.exp(-10.0 * dt)
    this.volumeRMS += (this.targetVolumeRMS - this.volumeRMS) * volLerpK
    const vol = this.volumeRMS

    // 自转：State 1/3 启用，方向随机可变
    if (s === UIState.Listening || s === UIState.ActionSpeaking) {
      if (time > this.nextRotationChange) {
        this.rotationSpeedX = (Math.random() - 0.5) * 0.3
        this.rotationSpeedY = (Math.random() - 0.5) * 0.4
        this.rotationSpeedZ = (Math.random() - 0.5) * 0.2
        this.nextRotationChange = time + 3 + Math.random() * 4
      }
      this.particleSystem.rotation.x += this.rotationSpeedX * dt
      this.particleSystem.rotation.y += this.rotationSpeedY * dt
      this.particleSystem.rotation.z += this.rotationSpeedZ * dt
    } else if (s === UIState.Standby || s === UIState.Processing) {
      this.particleSystem.rotation.x *= 0.98
      this.particleSystem.rotation.y *= 0.98
      this.particleSystem.rotation.z *= 0.98
      this.rotationSpeedX = 0
      this.rotationSpeedY = 0
      this.rotationSpeedZ = 0
    }

    if (this.alertPulse > 0) {
      this.alertPulse = Math.max(0, this.alertPulse - 0.02)
    }

    const posArr = this.positions
    const colArr = this.colors
    const sizeArr = this.sizes

    // 帧率无关 lerp：1 - e^(-k*dt)
    // 全状态 k=5（State 2 用 12）。高 k 会导致每帧位移过大，在 fade 0.15 下产生残影
    // State 1/3 的波峰卡顿改用速度预测解决（在目标计算中加入导数前馈）
    const posLerpK = (s === UIState.Processing) ? 1 - Math.exp(-12.0 * dt) : 1 - Math.exp(-5.0 * dt)
    const colorLerpK = 1 - Math.exp(-3.0 * dt)
    const sizeLerpK = 1 - Math.exp(-12.0 * dt)

    let targetR = this.targetHue.r
    let targetG = this.targetHue.g
    let targetB = this.targetHue.b

    if (this.alertPulse > 0) {
      const ap = this.alertPulse * (0.5 + 0.5 * Math.sin(time * 15))
      targetR = targetR + (COLOR_ALERT.r - targetR) * ap
      targetG = targetG + (COLOR_ALERT.g - targetG) * ap
      targetB = targetB + (COLOR_ALERT.b - targetB) * ap
    }

    for (let i = 0; i < PARTICLE_COUNT; i++) {
      const i3 = i * 3
      const p = this.particleData[i]
      let tx: number, ty: number, tz: number, ts: number

      if (s === UIState.Standby) {
        const breathe = Math.sin(time * 0.8 + p.noiseOffset) * 0.5
        const r = (p.baseRadius + 4.0) + breathe
        tx = r * Math.cos(p.sphereTheta) * Math.sin(p.spherePhi)
        ty = r * Math.sin(p.sphereTheta) * Math.sin(p.spherePhi)
        tz = r * Math.cos(p.spherePhi)
        ts = 0.6 + (breathe * 0.048)
      } else if (s === UIState.Listening) {
        const maxExpansion = vol * 40.0
        const waveFreq = 1.2
        const phaseOffset = p.baseRadius * 2.4
        const wavePhase = time * waveFreq - phaseOffset
        const breathe = (Math.sin(wavePhase) + 1.0) * 0.5
        const dynamicRadius = (p.baseRadius + 4.0) + (breathe * maxExpansion)
        // 速度预测：τ=0.12, 相位超前 42°, 抵消 56° 滞后中的 75%
        // 残留相位滞后 14°, 波峰处粒子速度 0.69A（vs 无预测 3.47A，降低 80%）
        // 减弱波峰附近"冲向最外圈"和"突然反向"的加速感
        const radiusVel = Math.cos(wavePhase) * 0.5 * waveFreq * maxExpansion
        const predictedRadius = dynamicRadius + radiusVel * 0.12
        tx = predictedRadius * Math.cos(p.sphereTheta) * Math.sin(p.spherePhi)
        ty = predictedRadius * Math.sin(p.sphereTheta) * Math.sin(p.spherePhi)
        tz = predictedRadius * Math.cos(p.spherePhi)
        ts = 0.7
      } else if (s === UIState.Processing) {
        p.currentOrbitAngle += p.orbitSpeed * dt
        let rawX = Math.cos(p.currentOrbitAngle) * p.orbitRadius
        let rawY = Math.sin(p.currentOrbitAngle) * p.orbitRadius
        let rawZ = Math.sin(p.currentOrbitAngle * 4 + p.noiseOffset) * 1.5
        let rot = this.rotatePoint3D(rawX, rawY, rawZ, p.tiltX, p.tiltZ)
        tx = rot.x
        ty = rot.y
        tz = rot.z
        ts = 0.6 + Math.sin(time * 8 + p.noiseOffset) * 0.225
      } else {
        // Speaking
        const maxExpansion = vol * 40.0
        const waveFreq = 1.8
        const phaseOffset = p.baseRadius * 0.6
        const wavePhase = time * waveFreq - phaseOffset
        const breathe = (Math.sin(wavePhase) + 1.0) * 0.5
        const dynamicRadius = (p.baseRadius + 4.0) + (breathe * maxExpansion)
        // 速度预测：τ=0.08, 相位超前 42°, 抵消 66° 滞后中的 64%
        // 残留相位滞后 24°, 波峰处粒子速度 1.255A（vs 无预测 4.18A，降低 70%）
        // 减弱波峰附近"冲向最外圈"和"突然反向"的加速感
        const radiusVel = Math.cos(wavePhase) * 0.5 * waveFreq * maxExpansion
        const predictedRadius = dynamicRadius + radiusVel * 0.08
        tx = predictedRadius * Math.cos(p.sphereTheta) * Math.sin(p.spherePhi)
        ty = predictedRadius * Math.sin(p.sphereTheta) * Math.sin(p.spherePhi)
        tz = predictedRadius * Math.cos(p.spherePhi)
        ts = 0.7
      }

      posArr[i3] += (tx - posArr[i3]) * posLerpK
      posArr[i3 + 1] += (ty - posArr[i3 + 1]) * posLerpK
      posArr[i3 + 2] += (tz - posArr[i3 + 2]) * posLerpK

      colArr[i3] += (targetR - colArr[i3]) * colorLerpK
      colArr[i3 + 1] += (targetG - colArr[i3 + 1]) * colorLerpK
      colArr[i3 + 2] += (targetB - colArr[i3 + 2]) * colorLerpK

      sizeArr[i] += (ts - sizeArr[i]) * sizeLerpK
    }

    this.geometry.attributes.position.needsUpdate = true
    this.geometry.attributes.color.needsUpdate = true
    this.geometry.attributes.size.needsUpdate = true

    // 渲染：1.残影覆盖 → 2.当前帧粒子
    this.renderer.render(this.fadeScene, this.camera)
    this.renderer.render(this.scene, this.camera)
  }
}
