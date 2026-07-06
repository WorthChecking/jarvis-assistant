vue_code = """<template>
  <div class="ai-assistant-container">
    <div class="canvas-wrapper" :class="{ 'active-border': currentState !== 0 }">
      <canvas ref="visualCanvas" :width="width" :height="height"></canvas>
    </div>

    <div class="mock-control-panel">
      <div class="panel-title">Trae 交互状态控制台 (V1.3)</div>
      <div class="btn-group">
        <button :class="{ active: currentState === 0 }" @click="setProjectState(0)">State 0: 待机 (Standby)</button>
        <button :class="{ active: currentState === 1 }" @click="setProjectState(1)">State 1: 聆听 (Listening)</button>
        <button :class="{ active: currentState === 2 }" @click="setProjectState(2)">State 2: 处理中 (Processing)</button>
        <button :class="{ active: currentState === 3 }" @click="setProjectState(3)">State 3: 播报 (Speaking)</button>
      </div>
      <div class="slider-group" v-if="currentState === 1 || currentState === 3">
        <label>模拟实时音量包络 (Volume): {{ mockVolume }}</label>
        <input type="range" min="0" max="100" v-model.number="mockVolume" />
      </div>
      <div class="status-text">
        状态对齐: <span class="highlight">State {{ currentState }}</span> | 
        视觉色系: <span class="highlight">{{ currentState >= 2 ? '橘黄流光 (Amber)' : '冰蓝全息 (Cyan)' }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue';

// 基础视口配置
const width = ref(400);
const height = ref(400);
const visualCanvas = ref(null);
let ctx = null;
let animationFrameId = null;

// 全局状态机映射
const currentState = ref(0);
const mockVolume = ref(0);

// 粒子系统底座（严格死守对象池红线，严禁动态垃圾回收 GC）
const PARTICLE_COUNT = 800; 
const particles = [];

// 3D 空间投影核心数学参数
const perspective = 300;
const centerX = width.value / 2;
const centerY = height.value / 2;

// 功耗优化控制
let lastFrameTime = 0;
const FPS_STANDBY = 15; // State 0 强行降频至 15fps，死守后台低功耗红线
const FPS_ACTIVE = 60;  // 活跃状态释放 60fps 全流畅流光

// 颜色通道插值缓动参数
let currentHue = 190; // 初始冰蓝色 (Cyan: ~190)
let targetHue = 190;

// 粒子实体对象池结构定义
class Particle {
  constructor() {
    this.reset();
  }

  reset() {
    // 基础 3D 笛卡尔坐标
    this.x = (Math.random() - 0.5) * 30;
    this.y = (Math.random() - 0.5) * 30;
    this.z = (Math.random() - 0.5) * 30;
    
    // 速度标量
    this.vx = 0;
    this.vy = 0;
    this.vz = 0;

    // 轨道动力学专用参数（用于状态2的交错立体轨道公转）
    this.orbitRadius = Math.random() * 110 + 35;
    this.orbitAngle = Math.random() * Math.PI * 2;
    this.orbitSpeed = (Math.random() * 0.05 + 0.02) * (Math.random() > 0.5 ? 1 : -1);
    
    // 3D 轨道空间倾角与偏航矩阵（构建立体织网感）
    this.pitch = (Math.random() - 0.5) * Math.PI * 0.7; 
    this.yaw = (Math.random() - 0.5) * Math.PI * 0.7;   

    // 基础视觉特征
    this.size = Math.random() * 1.6 + 0.4;
    this.alpha = Math.random() * 0.4 + 0.6;
    
    // 状态3专用的粒子个体系数
    this.burstSpeed = Math.random() * 0.1 + 0.05;
  }
}

// 初始化对象池
const initObjectPool = () => {
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    particles.push(new Particle());
  }
};

// 状态流转监听：平滑切换色系通道与窗口属性
watch(currentState, (newState) => {
  if (newState >= 2) {
    targetHue = 25;  // 强制过渡到温暖的橘黄色系 (Amber/Tangerine)
  } else {
    targetHue = 190; // 强制过渡到冷冽的冰蓝全息色系 (Cyan/White)
  }
  
  // Tauri 窗口事件穿透机制联动
  if (window.__TAURI__) {
    const isThrough = newState === 0; // 只有在 State 0 待机时允许鼠标穿透
    window.__TAURI__.invoke('toggle_click_through', { through: isThrough });
  }
});

// 核心渲染循环：包含空间物理换算与状态切换动效
const renderLoop = (timestamp) => {
  animationFrameId = requestAnimationFrame(renderLoop);

  // 资源红线截断：分状态控制帧率降低 CPU/GPU 空载开销
  const targetFps = currentState.value === 0 ? FPS_STANDBY : FPS_ACTIVE;
  const frameInterval = 1000 / targetFps;
  const elapsed = timestamp - lastFrameTime;

  if (elapsed < frameInterval) return;
  lastFrameTime = timestamp - (elapsed % frameInterval);

  // 全息流光残留特效：通过半透明矩形覆盖实现 Motion Blur
  ctx.fillStyle = 'rgba(10, 10, 13, 0.14)';
  ctx.fillRect(0, 0, width.value, height.value);

  // 色彩通道平滑过渡插值
  currentHue += (targetHue - currentHue) * 0.08;

  // 开启光学叠加混合模式，形成 Neon 强发光效果 (Bloom)
  ctx.globalCompositeOperation = 'lighter';

  // 迭代对象池中的粒子，应用对应的状态动力学方程
  for (let i = 0; i < particles.length; i++) {
    const p = particles[i];

    switch (currentState.value) {
      case 0: // ================== STATE 0: 待机 (Standby) ==================
        {
          // 逻辑对齐：冷色调、收缩为极紧密的静态微小核心，伴随极低频率的整体呼吸
          const breathFactor = Math.sin(timestamp * 0.0015) * 4;
          
          // 粒子向三维中心极速收敛
          const targetX = (Math.random() - 0.5) * 6 + (p.x > 0 ? 1 : -1) * breathFactor * 0.1;
          const targetY = (Math.random() - 0.5) * 6 + (p.y > 0 ? 1 : -1) * breathFactor * 0.1;
          const targetZ = (Math.random() - 0.5) * 6;

          p.x += (targetX - p.x) * 0.06;
          p.y += (targetY - p.y) * 0.06;
          p.z += (targetZ - p.z) * 0.06;
        }
        break;

      case 1: // ================== STATE 1: 聆听 (Listening) ==================
        {
          // 逻辑对齐：冷色调、核心轻微展开为散点，扩展半径与运动能量瞬时随音量振幅跳动
          const volAmp = mockVolume.value / 100;
          
          // 基于球坐标系为粒子分配基础发散目标
          const theta = Math.random() * Math.PI * 2;
          const phi = Math.acos((Math.random() * 2) - 1);
          // 扩散半径与音量强挂钩
          const currentRadius = 35 + volAmp * 85;

          const targetX = currentRadius * Math.sin(phi) * Math.cos(theta);
          const targetY = currentRadius * Math.sin(phi) * Math.sin(theta);
          const targetZ = currentRadius * Math.cos(phi);

          // 引入音量噪声抖动场 (Jitter)
          const jitter = volAmp * 6;
          p.x += (targetX - p.x) * 0.1 + (Math.random() - 0.5) * jitter;
          p.y += (targetY - p.y) * 0.1 + (Math.random() - 0.5) * jitter;
          p.z += (targetZ - p.z) * 0.1 + (Math.random() - 0.5) * jitter;
        }
        break;

      case 2: // ================== STATE 2: 处理中 (Processing) ==================
        {
          // 逻辑对齐：强制切换为橘黄色系，在不同倾角和深度的圆周轨道上作高速交错公转，无视音量
          p.orbitAngle += p.orbitSpeed;

          // 二维圆周运动叠加 3D 旋转矩阵参数（Pitch 与 Yaw 变换）
          let rx = p.orbitRadius * Math.cos(p.orbitAngle);
          let ry = p.orbitRadius * Math.sin(p.orbitAngle) * Math.cos(p.pitch);
          let rz = p.orbitRadius * Math.sin(p.orbitAngle) * Math.sin(p.pitch);

          // 叠加偏航角，使多轨道实现完全的错落交织
          let finalX = rx * Math.cos(p.yaw) - rz * Math.sin(p.yaw);
          let finalZ = rx * Math.sin(p.yaw) + rz * Math.cos(p.yaw);

          p.x += (finalX - p.x) * 0.1;
          p.y += (ry - p.y) * 0.1;
          p.z += (finalZ - p.z) * 0.1;

          // 保持中心有一个高密度的计算核心
          if (i % 6 === 0) {
            p.x += ((Math.random() - 0.5) * 8 - p.x) * 0.2;
            p.y += ((Math.random() - 0.5) * 8 - p.y) * 0.2;
            p.z += ((Math.random() - 0.5) * 8 - p.z) * 0.2;
          }
        }
        break;

      case 3: // ================== STATE 3: 播报 (Speaking) ==================
        {
          // 逻辑对齐：保持橘黄色系，视觉结构复刻状态1的展开散点态
          // 核心重构：随音量波幅产生突发性的【向外放射爆炸】或【向内重力坍缩】的强双向引力场交互
          const speakAmp = mockVolume.value / 100;
          
          // 利用正弦时间波形制造周期性的爆发/坍缩心跳感
          const isExploding = Math.sin(timestamp * 0.015) > -0.2;
          
          const theta = Math.random() * Math.PI * 2;
          const phi = Math.acos((Math.random() * 2) - 1);
          
          let dynamicRadius;
          if (isExploding) {
            // 向外爆发：音量越高，粒子被推向越远的极值边缘
            dynamicRadius = 30 + speakAmp * 120;
          } else {
            // 向内坍缩：高音量时向核心强力收紧，形成高能压缩态
            dynamicRadius = Math.max(5, 40 * (1 - speakAmp));
          }

          const targetX = dynamicRadius * Math.sin(phi) * Math.cos(theta);
          const targetY = dynamicRadius * Math.sin(phi) * Math.sin(theta);
          const targetZ = dynamicRadius * Math.cos(phi);

          // 随音量越高，粒子的物理缓动速度（爆发力）呈指数级上升
          const dynamicEase = 0.08 + speakAmp * p.burstSpeed;
          p.x += (targetX - p.x) * dynamicEase;
          p.y += (targetY - p.y) * dynamicEase;
          p.z += (targetZ - p.z) * dynamicEase;

          // 震荡波噪点补充
          if (speakAmp > 0.5) {
            p.x += (Math.random() - 0.5) * (speakAmp * 12);
            p.y += (Math.random() - 0.5) * (speakAmp * 12);
            p.z += (Math.random() - 0.5) * (speakAmp * 12);
          }
        }
        break;
    }

    // 5. 三维透视几何投影计算（Perspective Projection）
    // 通过 Z 轴位置计算缩放比例，完美映射“近大变亮、远小变暗”的真 3D 纵深交错感
    const scale = perspective / (perspective + p.z);
    const screenX = centerX + p.x * scale;
    const screenY = centerY + p.y * scale;

    // 视口边界裁剪安全拦截
    if (screenX >= 0 && screenX <= width.value && screenY >= 0 && screenY <= height.value) {
      const renderSize = Math.max(0.1, p.size * scale);
      // 计算基于距离的空间阿尔法通道暗化因子
      const alphaFactor = Math.min(1, Math.max(0.15, scale * p.alpha));

      // 绘制粒子核心亮斑
      ctx.beginPath();
      ctx.arc(screenX, screenY, renderSize, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${currentHue}, 95%, 65%, ${alphaFactor})`;
      ctx.fill();

      // 对 State 2 与 State 3 的高能粒子进行光学外晕（Glow/Bloom）补偿，提升电影质感
      if ((currentState.value === 2 || currentState.value === 3) && i % 12 === 0) {
        ctx.beginPath();
        ctx.arc(screenX, screenY, renderSize * 3.5, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${currentHue}, 100%, 70%, ${alphaFactor * 0.18})`;
        ctx.fill();
      }
    }
  }

  // 恢复默认画布合成属性，切断性能或渲染污染
  ctx.globalCompositeOperation = 'source-over';
};

// Mock 面板状态驱动函数
const setProjectState = (stateNum) => {
  currentState.value = stateNum;
  if (stateNum !== 1 && stateNum !== 3) {
    mockVolume.value = 0;
  } else {
    mockVolume.value = 50; // 默认初始化一个半响度包络
  }
};

// 组件生命周期守卫
onMounted(() => {
  ctx = visualCanvas.value.getContext('2d');
  initObjectPool();
  animationFrameId = requestAnimationFrame(renderLoop);
});

onBeforeUnmount(() => {
  if (animationFrameId) {
    cancelAnimationFrame(animationFrameId);
  }
});
</script>

<style scoped>
.ai-assistant-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  background-color: #0c0c0f;
  padding: 24px;
  border-radius: 16px;
  width: 100%;
  max-width: 460px;
  margin: 0 auto;
  box-shadow: 0 12px 40px rgba(0, 0, 0, 0.6);
  font-family: system-ui, -apple-system, sans-serif;
}

.canvas-wrapper {
  background-color: #040406;
  border: 1px solid #1f1f2e;
  border-radius: 50%;
  overflow: hidden;
  margin-bottom: 24px;
  box-shadow: inset 0 0 25px rgba(0, 255, 255, 0.03);
  transition: all 0.4s cubic-bezier(0.25, 0.8, 0.25, 1);
}

.canvas-wrapper.active-border {
  border-color: rgba(255, 140, 0, 0.35);
  box-shadow: 0 0 20px rgba(255, 140, 0, 0.05);
}

.mock-control-panel {
  width: 100%;
  background-color: #14141c;
  border: 1px solid #222230;
  border-radius: 10px;
  padding: 18px;
  color: #d1d5db;
}

.panel-title {
  font-size: 13px;
  font-weight: 600;
  color: #9ca3af;
  margin-bottom: 14px;
  text-transform: uppercase;
  letter-spacing: 1.5px;
}

.btn-group {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 10px;
  margin-bottom: 18px;
}

button {
  background-color: #1f1f2e;
  border: 1px solid #2e2e42;
  color: #9ca3af;
  padding: 10px 14px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 12px;
  text-align: left;
  transition: all 0.2s cubic-bezier(0.4, 0, 0.2, 1);
}

button:hover {
  background-color: #27273a;
  color: #ffffff;
  border-color: #3b3b54;
}

button.active {
  background-color: #ff8c00;
  border-color: #ffaa33;
  color: #050507;
  font-weight: 700;
  box-shadow: 0 0 12px rgba(255, 140, 0, 0.2);
}

.slider-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 18px;
}

.slider-group label {
  font-size: 12px;
  color: #9ca3af;
}

input[type="range"] {
  width: 100%;
  accent-color: #ff8c00;
  background: #1f1f2e;
  height: 6px;
  border-radius: 4px;
  outline: none;
  cursor: pointer;
}

.status-text {
  font-size: 11px;
  color: #6b7280;
  text-align: center;
  border-top: 1px solid #222230;
  padding-top: 12px;
}

.highlight {
  color: #ffaa33;
  font-weight: 600;
}
</style>
"""

markdown_content = f"""# 贾维斯视觉核心重构规范文档与源码契约 (PRD V1.3 - FrontEnd)

本技术文档及源码完全基于最新一轮确定的前端 3D 粒子流光交互系统规范编写。移除了任何历史残留的中间幻觉状态，精准收拢并实现了状态机制的高性能视觉渲染。

---

## 📐 本轮回答各状态动力学数学模型设计规范

### 1. State 0: 待机 (Standby) — 【已严格按本轮要求对齐】
* **色彩定义**：强制锁定为冷色系通道（冰蓝/全息白，`currentHue = 190`）。
* **空间结构**：凝聚在中心的一个极紧密、高密度的极小能量核（缩减三维笛卡尔随机域半径到 6px 以内）。
* **动力行为**：几乎完全静止，配合低频正弦波控制方程（`Math.sin(timestamp * 0.0015)`）执行微弱的往复呼吸运动。
* **功耗控制**：独立定时器拦截器强制降频至 **15fps**，主动释放系统 CPU 与显卡 3D 渲染开销，常驻后台不争抢资源。

### 2. State 1: 聆听 (Listening) — 【已严格按本轮要求对齐】
* **色彩定义**：保持冷色系通道（冰蓝/全息白）。
* **空间结构**：粒子核心打破内敛状态，向外轻微展开为三维散点群。
* **动力行为**：粒子的整体发散物理半径与单点运动动能，与 WebSocket 协议中高频下发的模拟实时音量数据（`mockVolume`）进行**毫秒级瞬时映射**。音量越高，球坐标发散场半径越大（`35 + volAmp * 85`），同时引入高频噪点场模拟麦克风音频拾取的能量感。

### 3. State 2: 处理中 (Processing) — 【包含完整框架】
* **色彩定义**：色彩体系平滑插值切换到温暖的橘黄色系（`currentHue = 25`）。
* **空间结构**：粒子重组为多条离散的、具备不同倾角（`pitch`）与偏航角（`yaw`）的**真三维同心圆周交错轨道**。
* **动力行为**：粒子完全无视音量输入，在各自的立体轨道上执行高帧率（60fps）高速公转。中心保持高密度核代表大脑正在进行深度计算，形成具有强烈空间深度的交错织网感。

### 4. State 3: 播报 (Speaking) — 【已严格按本轮要求对齐】
* **色彩定义**：稳定保持在温暖的橘黄色系（`currentHue = 25`）。
* **空间结构**：三维空间结构无缝解构，**完全复刻 State 1 (聆听) 的全方位展开散点态**，从而在视觉上形成连续的形态转换，消除闪跳。
* **动力行为**：其运动场引入了强烈的**双向重力引力场交互**。随模拟 TTS 音量数据的跳动，粒子产生高频、突发性的**向外放射爆炸（Explosion）**或**向内向心坍缩核心（Gravitational Collapse）**。音量越高，爆发和坍缩的响应速度与物理跨度呈指数级爆发，完美模拟发音口型与声波冲击。

---

## 💻 完整可直接替换的前端源码 (`AIVisualCore.vue`)

请在宿主机的工作区中，直接将以下经过性能与规范双重优化的完整 Vue 3 代码覆盖并替换原有的旧组件文件。