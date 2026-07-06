vue_code = """<template>
  <div class="ai-assistant-container">
    <div class="canvas-wrapper" :class="{ 'clickable': currentState !== 0 }">
      <canvas ref="visualCanvas" :width="width" :height="height"></canvas>
    </div>

    <div class="mock-control-panel">
      <div class="panel-title">Trae 调测控制台</div>
      <div class="btn-group">
        <button :class="{ active: currentState === 0 }" @click="setProjectState(0)">State 0: 待机</button>
        <button :class="{ active: currentState === 1 }" @click="setProjectState(1)">State 1: 聆听</button>
        <button :class="{ active: currentState === 2 }" @click="setProjectState(2)">State 2: 处理中</button>
        <button :class="{ active: currentState === 3 }" @click="setProjectState(3)">State 3: 播报</button>
      </div>
      <div class="slider-group" v-if="currentState === 1 || currentState === 3">
        <label>模拟实时音量 (RMS): {{ mockVolume }}</label>
        <input type="range" min="0" max="100" v-model.number="mockVolume" />
      </div>
      <div class="status-text">
        当前状态码: <span class="highlight">{{ currentState }}</span> | 
        当前色系: <span class="highlight">{{ currentState >= 2 ? '橘黄流光' : '冰蓝全息' }}</span>
      </div>
    </div>
  </div>
</template>

<script setup>
import { ref, onMounted, onBeforeUnmount, watch } from 'vue';

// 基础配置
const width = ref(400);
const height = ref(400);
const visualCanvas = ref(null);
let ctx = null;
let animationFrameId = null;

// 状态管理 (0: 待机, 1: 聆听, 2: 处理中, 3: 播报)
const currentState = ref(0);
const mockVolume = ref(0);

// 粒子系统核心配置（严格死守性能红线：使用对象池，严禁动态GC）
const PARTICLE_COUNT = 600;
const particles = [];

// 3D 空间透视参数
const perspective = 300;
const centerX = width.value / 2;
const centerY = height.value / 2;

// 动态帧率控制
let lastFrameTime = 0;
const FPS_STANDBY = 15; // 待机状态强制降频至 15fps，死守资源红线
const FPS_ACTIVE = 60;  // 活跃状态保持 60fps 流畅流光

// 颜色平滑渐变控制 (插值过渡)
let currentHue = 190; // 初始冰蓝色 (Cyan)
let targetHue = 190;

// 粒子对象池初始化结构
class Particle {
  constructor() {
    this.reset();
  }

  reset() {
    // 基础三维空间坐标
    this.x = (Math.random() - 0.5) * 40;
    this.y = (Math.random() - 0.5) * 40;
    this.z = (Math.random() - 0.5) * 40;
    
    // 速度矢量
    this.vx = 0;
    this.vy = 0;
    this.vz = 0;

    // 独立轨道动力学参数 (用于状态2的多同心圆交错环绕)
    this.orbitRadius = Math.random() * 100 + 30; // 离散轨道半径
    this.orbitAngle = Math.random() * Math.PI * 2; // 初始公转角度
    this.orbitSpeed = (Math.random() * 0.04 + 0.02) * (Math.random() > 0.5 ? 1 : -1); // 旋转速度与方向
    
    // 轨道三维倾角 (实现错落交织的关键数学矩阵参数)
    this.pitch = (Math.random() - 0.5) * Math.PI * 0.6; // X轴倾角
    this.yaw = (Math.random() - 0.5) * Math.PI * 0.6;   // Y轴倾角

    // 粒子自身视觉属性
    this.size = Math.random() * 1.5 + 0.5;
    this.alpha = Math.random() * 0.5 + 0.5;
  }
}

// 初始化对象池
const initObjectPool = () => {
  for (let i = 0; i < PARTICLE_COUNT; i++) {
    particles.push(new Particle());
  }
};

// 状态切换监听：执行逻辑纠偏与色系目标设定
watch(currentState, (newState) => {
  if (newState >= 2) {
    targetHue = 25; // 橘黄色系 (Orange/Amber)
  } else {
    targetHue = 190; // 冰蓝色系 (Cyan)
  }
  
  // 切换状态时重置粒子部分动力学状态，防止状态切换时物理突变
  particles.forEach(p => {
    if (newState === 0) {
      // 待机状态：收缩回核心，减速
      p.vx = (Math.random() - 0.5) * 0.5;
      p.vy = (Math.random() - 0.5) * 0.5;
      p.vz = (Math.random() - 0.5) * 0.5;
    }
  });
  
  // Tauri 窗口事件穿透联动（此处预留原生接口调用）
  if (window.__TAURI__) {
    // State 0 鼠标穿透，不阻挡操作；1,2,3 恢复拦截
    const isThrough = newState === 0;
    window.__TAURI__.invoke('toggle_click_through', { through: isThrough });
  }
});

// 核心渲染流 (每帧计算与物理投影)
const renderLoop = (timestamp) => {
  animationFrameId = requestAnimationFrame(renderLoop);

  // 动态计算帧率控制，死守功耗红线
  const targetFps = currentState.value === 0 ? FPS_STANDBY : FPS_ACTIVE;
  const frameInterval = 1000 / targetFps;
  const elapsed = timestamp - lastFrameTime;

  if (elapsed < frameInterval) return;
  lastFrameTime = timestamp - (elapsed % frameInterval);

  // 1. 全息视觉特效核心：动态拖尾 (Motion Blur) 
  // 严禁使用 clearRect，采用半透明覆盖，保留上一帧粒子10%的流光残影
  ctx.fillStyle = 'rgba(10, 10, 12, 0.12)';
  ctx.fillRect(0, 0, width.value, height.value);

  // 2. 颜色通道平滑插值 (防止颜色突变)
  currentHue += (targetHue - currentHue) * 0.08;

  // 3. 开启全息光学叠加合成模式 (Bloom 效果)
  ctx.globalCompositeOperation = 'lighter';

  // 4. 遍历粒子对象池执行动力学矩阵计算
  for (let i = 0; i < particles.length; i++) {
    const p = particles[i];

    // 分状态动力学核心状态机逻辑
    switch (currentState.value) {
      case 0: // State 0: 待机状态 (低能耗慢速休眠核心)
        {
          const targetX = (Math.random() - 0.5) * 8;
          const targetY = (Math.random() - 0.5) * 8;
          const targetZ = (Math.random() - 0.5) * 8;
          // 引入微弱呼吸缓动
          const breath = Math.sin(timestamp * 0.002) * 5;
          p.x += (targetX - p.x) * 0.05 + Math.sin(i) * 0.01;
          p.y += (targetY - p.y) * 0.05 + Math.cos(i) * 0.01;
          p.z += (targetZ - p.z) * 0.05;
          
          // 呼吸波纹扩散
          p.x += (p.x > 0 ? 1 : -1) * breath * 0.005;
          p.y += (p.y > 0 ? 1 : -1) * breath * 0.005;
        }
        break;

      case 1: // State 1: 聆听状态 (冷色散点随实时音量扩展半径)
        {
          const volFactor = mockVolume.value / 100;
          // 基础发散速度 + 音量扰动产生的爆破力
          const angle = Math.random() * Math.PI * 2;
          const radius = Math.random() * (40 + volFactor * 90);
          
          const targetX = Math.cos(angle) * radius;
          const targetY = Math.sin(angle) * radius;
          const targetZ = (Math.random() - 0.5) * (30 + volFactor * 50);

          p.x += (targetX - p.x) * 0.1 + (Math.random() - 0.5) * (volFactor * 8);
          p.y += (targetY - p.y) * 0.1 + (Math.random() - 0.5) * (volFactor * 8);
          p.z += (targetZ - p.z) * 0.1;
        }
        break;

      case 2: // State 2: 处理中状态 (核心修订：橘黄色多轨道三维圆周交错环绕)
        {
          // 公转角度递增
          p.orbitAngle += p.orbitSpeed;
          
          // 在标准的二维圆周运动基础上 ($x = r*cos\theta, y = r*sin\theta$) 引入 3D 倾角控制矩阵
          // 模拟卫星围绕中心核做多角度错落环绕
          let rawX = p.orbitRadius * Math.cos(p.orbitAngle);
          let rawY = p.orbitRadius * Math.sin(p.orbitAngle) * Math.cos(p.pitch);
          let rawZ = p.orbitRadius * Math.sin(p.orbitAngle) * Math.sin(p.pitch);

          // 引入围绕Y轴的偏航矩阵（Yaw）变换，使轨道全面交错
          let rotatedX = rawX * Math.cos(p.yaw) - rawZ * Math.sin(p.yaw);
          let rotatedZ = rawX * Math.sin(p.yaw) + rawZ * Math.cos(p.yaw);

          // 平滑过渡到三维错落轨道坐标
          p.x += (rotatedX - p.x) * 0.1;
          p.y += (rawY - p.y) * 0.1;
          p.z += (rotatedZ - p.z) * 0.1;

          // 保持一个中心高密度核心 (模拟高负载运转的大脑)
          if (i % 8 === 0) {
            p.x += ((Math.random() - 0.5) * 6 - p.x) * 0.2;
            p.y += ((Math.random() - 0.5) * 6 - p.y) * 0.2;
            p.z += ((Math.random() - 0.5) * 6 - p.z) * 0.2;
          }
        }
        break;

      case 3: // State 3: 播报状态 (核心修订：橘黄色系形态复刻状态1，随TTS音量突发扩张)
        {
          const ttsFactor = mockVolume.value / 100;
          const angle = Math.random() * Math.PI * 2;
          // 基础扩展范围 + 极强烈的向外排斥力
          const radius = Math.random() * (35 + ttsFactor * 110);
          
          const targetX = Math.cos(angle) * radius;
          const targetY = Math.sin(angle) * radius;
          const targetZ = (Math.random() - 0.5) * (20 + ttsFactor * 60);

          // 模拟声波震荡的强冲击感
          p.x += (targetX - p.x) * 0.15 + (Math.random() - 0.5) * (ttsFactor * 12);
          p.y += (targetY - p.y) * 0.15 + (Math.random() - 0.5) * (ttsFactor * 12);
          p.z += (targetZ - p.z) * 0.15;
        }
        break;
    }

    // 5. 三维空间投影与空间透视感处理 (核心视觉深度对齐)
    // 根据Z轴深度计算缩放因子与亮度因子，实现“近大亮，远小暗”的空间交错感
    const scale = perspective / (perspective + p.z);
    const screenX = centerX + p.x * scale;
    const screenY = centerY + p.y * scale;

    // 剔除越界投影点
    if (screenX >= 0 && screenX <= width.value && screenY >= 0 && screenY <= height.value) {
      // 深度渲染尺寸计算
      const renderSize = Math.max(0.1, p.size * scale * (currentState.value === 2 ? 1.2 : 1));
      // 越靠近观察者（Z越小），光斑越亮；越远离（Z越大），光斑越暗
      const brightness = Math.min(1, Math.max(0.2, scale * p.alpha));

      // 绘制全息粒子光斑
      ctx.beginPath();
      ctx.arc(screenX, screenY, renderSize, 0, Math.PI * 2);
      ctx.fillStyle = `hsla(${currentHue}, 95%, 65%, ${brightness})`;
      ctx.fill();

      // 在处理中和播报状态下，为核心粒子额外绘制高光外圈以增强能量感
      if ((currentState.value === 2 || currentState.value === 3) && i % 15 === 0) {
        ctx.beginPath();
        ctx.arc(screenX, screenY, renderSize * 3, 0, Math.PI * 2);
        ctx.fillStyle = `hsla(${currentHue}, 100%, 70%, ${brightness * 0.15})`;
        ctx.fill();
      }
    }
  }

  // 恢复默认合成模式，防止污染外部组件
  ctx.globalCompositeOperation = 'source-over';
};

// 测验控制面板交互函数
const setProjectState = (stateNum) => {
  currentState.value = stateNum;
  if (stateNum !== 1 && stateNum !== 3) {
    mockVolume.value = 0; // 非音频状态重置模拟音量
  } else {
    mockVolume.value = 45; // 音频状态初始化默认响度
  }
};

// 生命周期挂载与卸载
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
  background-color: #0a0a0c;
  padding: 20px;
  border-radius: 12px;
  width: 100%;
  max-width: 450px;
  margin: 0 auto;
  box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
}

.canvas-wrapper {
  background-color: #050507;
  border: 1px solid #1a1a24;
  border-radius: 50%;
  overflow: hidden;
  margin-bottom: 20px;
  box-shadow: inset 0 0 20px rgba(0, 255, 255, 0.05);
  transition: border-color 0.3s ease;
}

.canvas-wrapper.clickable {
  border-color: rgba(255, 140, 0, 0.3);
}

.mock-control-panel {
  width: 100%;
  background-color: #121218;
  border: 1px solid #1c1c28;
  border-radius: 8px;
  padding: 16px;
  color: #cdd1de;
}

.panel-title {
  font-size: 14px;
  font-weight: 600;
  color: #8f95a5;
  margin-bottom: 12px;
  text-transform: uppercase;
  letter-spacing: 1px;
}

.btn-group {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
  margin-bottom: 16px;
}

button {
  background-color: #1c1c24;
  border: 1px solid #2d2d3d;
  color: #a2a8b8;
  padding: 8px 12px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  transition: all 0.2s ease;
}

button:hover {
  background-color: #252530;
  color: #ffffff;
}

button.active {
  background-color: #ff8c00;
  border-color: #ffaa33;
  color: #000000;
  font-weight: 600;
}

.slider-group {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 16px;
}

.slider-group label {
  font-size: 12px;
  color: #8f95a5;
}

input[type="range"] {
  width: 100%;
  accent-color: #ff8c00;
  background: #1c1c24;
  height: 6px;
  border-radius: 3px;
  outline: none;
}

.status-text {
  font-size: 11px;
  color: #6c7282;
  text-align: center;
  border-top: 1px solid #1c1c28;
  padding-top: 10px;
}

.highlight {
  color: #ffaa33;
  font-weight: 600;
}
</style>
"""

markdown_content = f"""# PC桌面端智能悬浮窗助手 - 前端核心视觉粒子流光组件 (Vue 3 SFC)

本组件严格按照最新的架构规范与变更契约（PRD V1.2 及通信协议）编写。组件死守系统功耗红线与显存防卷防爆防抖红线，采用纯单文件结构（SFC），可在 Vue 3 + Vite + Tauri 桌面环境中无缝开箱即用。

## 🛠️ 核心视觉技术特性与对齐规范
1. **状态机强对齐**：强制将状态锁定为 `0`, `1`, `2`, `3` 四个物理通道，移除了任何多余的幻觉逻辑态，完美适配后端 WebSocket 指令映射。
2. **三维矩阵投影轨道 (State 2)**：在 `State 2 (处理中)` 时，色彩切换为**橘黄色**。采用 3D 圆周公转方程叠加倾角与偏航变换（Pitch & Yaw Rotation Matrix），利用 Canvas 2D 纯数学手段进行空间深度投影（Perspective Projection），实现了**多轨道离散、同心且三维交错圆周环绕**的极客计算感。
3. **形态动态转换 (State 3)**：在 `State 3 (播报)` 时，保持**橘黄色**。粒子空间形态无缝解构并复刻 `State 1 (聆听)` 的散点膨胀斥力场模型，振幅与实时音量数据高度敏感突发响应，避免了闪跳断层。
4. **性能红线兜底**：
   - **对象池（Object Pool）**：固定 `600` 个粒子常驻内存复用，渲染循环中没有任何 `new`、`delete` 或 `splice` 引发 V8 垃圾回收（GC）导致掉帧。
   - **休眠降频**：`State 0 (待机)` 状态下主动控制帧率为 `15fps`，其余时间跑满 `60fps`，将空载系统资源开销压缩至极限。
   - **拖尾与全息**：使用黑色 `0.12` 透明度覆盖实现流光残影（Motion Blur），开启 `globalCompositeOperation = 'lighter'` 模拟光学亮斑重叠（Bloom 特效）。

---

## 💻 完整前端组件源码 (`AIVisualCore.vue`)