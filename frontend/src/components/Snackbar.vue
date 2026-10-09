<script setup lang="ts">
import { useToast } from "../composables/useToast";

const { message, visible } = useToast();
</script>

<template>
  <div class="snackbar" :class="{ show: visible }" role="status">{{ message }}</div>
</template>

<style scoped>
.snackbar {
  position: fixed; left: 16px; right: 16px; bottom: calc(16px + env(safe-area-inset-bottom)); z-index: 10;
  max-width: 560px; margin: 0 auto; padding: 14px 16px; border-radius: var(--md-sys-shape-corner-extra-small);
  background: var(--md-sys-color-inverse-surface); color: var(--md-sys-color-inverse-on-surface);
  box-shadow: 0 3px 8px 3px var(--md-elevation-shadow);
  font: var(--md-sys-typescale-body-medium);
  letter-spacing: var(--md-sys-typescale-body-medium-tracking);
  transform: translateY(160%);
  transition: transform var(--md-sys-motion-duration-medium2) var(--md-sys-motion-easing-emphasized-decelerate);
}
.snackbar.show { transform: none; }
</style>
