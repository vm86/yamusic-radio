<script setup lang="ts">
// The bands of the music as bars. They move with the sound, 30 times a second.
defineProps<{ bands: number[] }>();
</script>

<template>
  <div class="spectrum" aria-hidden="true">
    <i v-for="(value, index) in bands" :key="index" :style="{ transform: `scaleY(${Math.max(value, 0.04)})` }" />
  </div>
</template>

<style scoped>
.spectrum { display: flex; align-items: flex-end; gap: 4px; height: 40px; }
.spectrum i {
  flex: 1; height: 100%; transform-origin: bottom;
  border-radius: var(--md-sys-shape-corner-extra-small);
  background: var(--md-sys-color-primary);
  opacity: 0.6;
}
/* The motion is the content here, and it can be a lot of it. */
@media (prefers-reduced-motion: reduce) { .spectrum { display: none; } }
</style>
