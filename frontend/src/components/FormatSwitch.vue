<script setup lang="ts">
import { STREAM_PATHS, type StreamFormat } from "../composables/usePlayer";
import { t } from "../i18n";
import Icon from "./Icon.vue";

// An M3 segmented button, one of two: which stream the page listens to.
const format = defineModel<StreamFormat>({ required: true });
const formats = Object.keys(STREAM_PATHS) as StreamFormat[];
const titles = (id: StreamFormat): string => t(id === "mp3" ? "player.formatMp3" : "player.formatFlac");
</script>

<template>
  <div class="segments" role="radiogroup" :aria-label="t('player.format')">
    <button
      v-for="id in formats"
      :key="id"
      class="segment"
      type="button"
      role="radio"
      :aria-checked="format === id"
      :title="titles(id)"
      :aria-label="titles(id)"
      @click="format = id"
    >
      <Icon v-if="format === id" name="check" />{{ id.toUpperCase() }}
    </button>
  </div>
</template>

<style scoped>
.segments {
  display: flex; justify-self: center; height: 40px;
  border: 1px solid var(--md-sys-color-outline); border-radius: var(--md-sys-shape-corner-full);
  overflow: hidden;
}
.segment {
  position: relative; display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  min-width: 88px; padding: 0 16px; border: 0; cursor: pointer; background: transparent;
  color: var(--md-sys-color-on-surface);
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
}
.segment + .segment { border-left: 1px solid var(--md-sys-color-outline); }
.segment[aria-checked="true"] {
  background: var(--md-sys-color-secondary-container); color: var(--md-sys-color-on-secondary-container);
}
.segment .icon { width: 18px; height: 18px; }
/* The touch target is 48dp tall */
.segment::before { content: ""; position: absolute; inset: -4px 0; }
.segment:hover::after, .segment:focus-visible::after {
  content: ""; position: absolute; inset: 0; background: currentColor; opacity: 0.08; pointer-events: none;
}
.segment:focus-visible { outline-offset: -2px; }
</style>
