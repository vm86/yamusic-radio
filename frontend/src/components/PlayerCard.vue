<script setup lang="ts">
import { computed } from "vue";
import type { NowTrack } from "../api";
import { MOODS } from "../constants";
import { clock } from "../format";
import Icon from "./Icon.vue";
import KeyTag from "./KeyTag.vue";
import VinylRecord from "./VinylRecord.vue";

const props = defineProps<{
  now: NowTrack | null;
  elapsed: number;
  playing: boolean;
  label: string;
}>();
const volume = defineModel<number>("volume", { required: true });
defineEmits<{ toggle: [] }>();

const title = computed(() => props.now?.title ?? "Ничего не играет");
const artists = computed(() => {
  if (props.now) return props.now.artists.join(", ") || "—";
  return props.playing ? "Готовим первый трек…" : "Нажмите «Слушать»";
});
const progress = computed(() => {
  const total = props.now?.duration_seconds ?? 0;
  return total ? (props.elapsed / total) * 100 : 0;
});
</script>

<template>
  <section class="card player">
    <div class="cover">
      <VinylRecord :cover="now?.cover ?? ''" :spinning="playing" />
    </div>
    <div class="meta" aria-live="polite" aria-atomic="true">
      <h2 class="title">{{ title }}</h2>
      <p class="artists">{{ artists }}</p>
      <div v-if="now" class="facts">
        <span v-if="now.bpm" class="assist">{{ Math.round(now.bpm) }} BPM</span>
        <KeyTag v-if="now.camelot" :camelot="now.camelot" :key-name="now.key" kind="assist" />
        <span v-if="MOODS[now.mood]" class="assist">{{ MOODS[now.mood] }}</span>
        <span class="assist">{{ Math.round(now.loudness_db) }} дБ</span>
      </div>
    </div>
    <div class="progress">
      <div
        class="bar"
        role="progressbar"
        aria-label="Позиция в треке"
        aria-valuemin="0"
        :aria-valuenow="Math.round(elapsed)"
        :aria-valuemax="Math.round(now?.duration_seconds ?? 0)"
        :aria-valuetext="now ? `${clock(elapsed)} из ${clock(now.duration_seconds)}` : undefined"
      >
        <i :style="{ width: `${progress}%` }" />
      </div>
      <div class="times">
        <span>{{ clock(now ? elapsed : 0) }}</span>
        <span>{{ clock(now?.duration_seconds) }}</span>
      </div>
    </div>
    <div class="controls">
      <button class="fab" type="button" :aria-label="label" @click="$emit('toggle')">
        <Icon :name="playing ? 'pause' : 'play'" />
      </button>
      <label class="volume">
        <Icon name="vol" />
        <input
          v-model.number="volume"
          type="range"
          min="0"
          max="100"
          aria-label="Громкость"
          :style="{ '--fill': `${volume}%` }"
        />
      </label>
    </div>
  </section>
</template>

<style scoped>
.player { display: grid; gap: 20px; }
.cover {
  aspect-ratio: 1; width: 100%; max-width: 320px; margin: 0 auto;
  border-radius: var(--md-sys-shape-corner-large);
  background:
    radial-gradient(circle at 30% 25%, color-mix(in srgb, var(--md-sys-color-primary) 55%, transparent), transparent 60%),
    linear-gradient(145deg, var(--md-sys-color-primary-container), var(--md-sys-color-secondary-container));
  display: grid; place-items: center; color: var(--md-sys-color-on-primary-container);
  overflow: hidden;
}
.cover > * { width: 78%; }
.meta { text-align: center; min-width: 0; }
.title {
  font: var(--md-sys-typescale-headline-small);
  letter-spacing: var(--md-sys-typescale-headline-small-tracking);
  color: var(--md-sys-color-on-surface);
  overflow-wrap: anywhere;
}
.artists { margin-top: 4px; color: var(--md-sys-color-on-surface-variant); overflow-wrap: anywhere; }
.facts { display: flex; flex-wrap: wrap; gap: 8px; justify-content: center; margin-top: 12px; }
.progress { display: grid; gap: 4px; }
.bar { height: 4px; border-radius: var(--md-sys-shape-corner-full); background: var(--md-sys-color-secondary-container); overflow: hidden; }
.bar > i {
  display: block; height: 100%; background: var(--md-sys-color-primary); border-radius: var(--md-sys-shape-corner-full);
  transition: width var(--md-sys-motion-duration-medium4) var(--md-sys-motion-easing-linear);
}
.times {
  display: flex; justify-content: space-between;
  font: var(--md-sys-typescale-body-small);
  letter-spacing: var(--md-sys-typescale-body-small-tracking);
  color: var(--md-sys-color-on-surface-variant);
}
.controls { display: flex; align-items: center; gap: 16px; }
.fab {
  position: relative; width: 64px; height: 64px; border: 0; border-radius: var(--md-sys-shape-corner-large-increased); flex: none;
  background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container);
  display: grid; place-items: center; cursor: pointer;
  box-shadow: 0 1px 3px 1px var(--md-elevation-shadow);
}
.fab .icon { width: 32px; height: 32px; }
.volume { flex: 1; display: flex; align-items: center; gap: 12px; color: var(--md-sys-color-on-surface-variant); min-width: 0; }

/* M3 slider */
input[type="range"] {
  -webkit-appearance: none; appearance: none; flex: 1; min-width: 0; height: 48px; margin: 0;
  background: transparent; cursor: pointer;
}
input[type="range"]::-webkit-slider-runnable-track {
  height: 16px; border-radius: var(--md-sys-shape-corner-small);
  background: linear-gradient(to right, var(--md-sys-color-primary) var(--fill, 100%), var(--md-sys-color-secondary-container) var(--fill, 100%));
}
input[type="range"]::-webkit-slider-thumb {
  -webkit-appearance: none; width: 4px; height: 44px; margin-top: -14px; border-radius: var(--md-sys-shape-corner-extra-small);
  background: var(--md-sys-color-primary); border: 0;
}
input[type="range"]::-moz-range-track { height: 16px; border-radius: var(--md-sys-shape-corner-small); background: var(--md-sys-color-secondary-container); }
input[type="range"]::-moz-range-progress { height: 16px; border-radius: var(--md-sys-shape-corner-small); background: var(--md-sys-color-primary); }
input[type="range"]::-moz-range-thumb { width: 4px; height: 44px; border-radius: var(--md-sys-shape-corner-extra-small); background: var(--md-sys-color-primary); border: 0; }
</style>
