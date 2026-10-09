<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { coverUrl } from "../format";

// A record: the cover is the label in its middle, the whole disc turns while
// the radio plays and keeps its angle when it stops.
const props = defineProps<{ cover: string; spinning: boolean }>();

const failed = ref(false);
watch(() => props.cover, () => (failed.value = false));
// The label is about a third of the disc: ask for twice the pixels it shows.
const src = computed(() => (failed.value ? "" : coverUrl(props.cover, 200)));
</script>

<template>
  <div class="vinyl" aria-hidden="true">
    <div class="disc" :class="{ spinning }">
      <div class="label">
        <img
          v-if="src"
          :src="src"
          alt=""
          draggable="false"
          referrerpolicy="no-referrer"
          @error="failed = true"
        />
      </div>
      <div class="hole" />
    </div>
  </div>
</template>

<style scoped>
.vinyl {
  position: relative; width: 100%; aspect-ratio: 1; border-radius: var(--md-sys-shape-corner-full);
  box-shadow: 0 2px 8px 2px var(--md-elevation-shadow);
}
/* The light on the disc does not turn with it */
.vinyl::after {
  content: ""; position: absolute; inset: 0; border-radius: inherit; pointer-events: none;
  background: conic-gradient(
    from 20deg,
    transparent 0 14%,
    color-mix(in srgb, var(--md-sys-color-primary-container) 22%, transparent) 20%,
    transparent 28% 64%,
    color-mix(in srgb, var(--md-sys-color-primary-container) 16%, transparent) 70%,
    transparent 78%
  );
}
.disc {
  position: absolute; inset: 0; border-radius: inherit; display: grid; place-items: center;
  background:
    repeating-radial-gradient(
      circle,
      color-mix(in srgb, var(--md-sys-color-primary-container) 10%, transparent) 0 1px,
      transparent 1px 3px
    ),
    radial-gradient(circle, color-mix(in srgb, var(--md-sys-color-scrim) 86%, var(--md-sys-color-primary)), var(--md-sys-color-scrim));
  animation: spin var(--app-motion-spin) var(--md-sys-motion-easing-linear) infinite;
  animation-play-state: paused;
}
.disc.spinning { animation-play-state: running; }
.label {
  grid-area: 1 / 1; width: 36%; aspect-ratio: 1; border-radius: var(--md-sys-shape-corner-full); overflow: hidden;
  background: linear-gradient(145deg, var(--md-sys-color-primary), var(--md-sys-color-tertiary));
  box-shadow: 0 0 0 2px color-mix(in srgb, var(--md-sys-color-scrim) 60%, transparent);
}
.label img { display: block; width: 100%; height: 100%; object-fit: cover; }
.hole {
  grid-area: 1 / 1; width: 4%; aspect-ratio: 1; border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-surface);
}
</style>
