<template>
  <div class="ai-visual-core" ref="containerRef"></div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from 'vue'
import { ParticleEngine } from '../engine/ParticleEngine'
import { UIState } from '../types'

const props = defineProps({
  state: { type: Number as () => number, default: 0, validator: (v: number) => v >= 0 && v <= 3 },
  volumeRms: { type: Number as () => number, default: 0 },
})

const containerRef = ref<HTMLElement | null>(null)
let engine: ParticleEngine | null = null

watch(() => props.state, (newState) => {
  if (engine && newState >= 0 && newState <= 3) {
    engine.setState(newState as UIState)
  }

  if ((window as any).__TAURI__) {
    const isThrough = newState === 0
    ;(window as any).__TAURI__.invoke('toggle_click_through', { through: isThrough })
  }
})

watch(() => props.volumeRms, (v) => {
  if (engine) {
    engine.setVolume(v)
  }
})

function handleResize() {
  if (engine) {
    engine.resize()
  }
}

onMounted(() => {
  if (containerRef.value) {
    engine = new ParticleEngine(containerRef.value)
    engine.setState(props.state as UIState)
    engine.setVolume(props.volumeRms)
    engine.start()
  }
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  if (engine) {
    engine.dispose()
    engine = null
  }
})
</script>

<style scoped>
.ai-visual-core {
  width: 100%;
  height: 100%;
  background: #000;
  overflow: hidden;
}

.ai-visual-core canvas {
  display: block;
  width: 100% !important;
  height: 100% !important;
}
</style>
