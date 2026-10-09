<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { APP_TITLE } from "../constants";
import Icon from "./Icon.vue";
import ThemeButton from "./ThemeButton.vue";

defineProps<{ playing: boolean }>();

// The bar takes a tonal surface once the page is scrolled.
const scrolled = ref(false);
const onScroll = (): void => {
  scrolled.value = window.scrollY > 0;
};
onMounted(() => window.addEventListener("scroll", onScroll, { passive: true }));
onBeforeUnmount(() => window.removeEventListener("scroll", onScroll));
</script>

<template>
  <header class="app-bar" :class="{ scrolled }">
    <Icon name="vinyl" class="logo" />
    <h1>{{ APP_TITLE }}</h1>
    <span class="live" :class="{ on: playing }" role="status">
      {{ playing ? "В эфире" : "Пауза" }}
    </span>
    <ThemeButton />
  </header>
</template>

<style scoped>
.app-bar {
  position: sticky; top: 0; z-index: 5;
  display: flex; align-items: center; gap: 12px;
  height: 64px; padding: 0 16px;
  background: var(--md-sys-color-surface);
  transition: background-color var(--md-sys-motion-duration-medium2) var(--md-sys-motion-easing-emphasized);
}
.app-bar.scrolled { background: var(--md-sys-color-surface-container); }
.logo { color: var(--md-sys-color-primary); }
h1 {
  font: var(--md-sys-typescale-title-large);
  letter-spacing: var(--md-sys-typescale-title-large-tracking);
  flex: 1; min-width: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
@media (max-width: 420px) { h1 { font-size: 18px; } }
.live {
  display: inline-flex; align-items: center; gap: 8px;
  height: 32px; padding: 0 12px; border-radius: var(--md-sys-shape-corner-small);
  background: var(--md-sys-color-surface-container-high);
  color: var(--md-sys-color-on-surface-variant);
  font: var(--md-sys-typescale-label-medium);
  letter-spacing: var(--md-sys-typescale-label-medium-tracking);
}
.live::before { content: ""; width: 8px; height: 8px; border-radius: var(--md-sys-shape-corner-full); background: var(--md-sys-color-outline); }
.live.on { background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container); }
.live.on::before { background: var(--md-sys-color-error); animation: pulse var(--app-motion-pulse) var(--md-sys-motion-easing-standard) infinite; }
</style>
