<script setup lang="ts">
import type { TrackInfo } from "../api";
import { MOODS } from "../constants";
import { clock } from "../format";
import KeyTag from "./KeyTag.vue";

defineProps<{
  tracks: TrackInfo[];
  empty: string;
  kind: "past" | "queue";
}>();

/** "−2", "−1" for what was played, "+1", "+2" for what comes. */
function badge(kind: "past" | "queue", index: number, count: number): string {
  return kind === "past" ? `−${count - index}` : `+${index + 1}`;
}

function sub(track: TrackInfo): string {
  return [track.artists.join(", "), MOODS[track.mood]].filter(Boolean).join(" · ") || "—";
}
</script>

<template>
  <ul class="list">
    <li v-if="!tracks.length" class="empty">{{ empty }}</li>
    <li
      v-for="(track, index) in tracks"
      :key="`${track.track_id}-${index}`"
      class="item"
      :class="kind === 'past' ? 'past' : track.decided ? 'ready' : 'forecast'"
    >
      <div class="leading">
        {{ badge(kind, index, tracks.length) }}
        <!-- The dashed ring says it to the eye, this to a screen reader. -->
        <span v-if="kind === 'past'" class="sr">, уже играл</span>
        <span v-else-if="!track.decided" class="sr">, прогноз</span>
      </div>
      <div class="text">
        <div class="t" :title="track.title">{{ track.title }}</div>
        <div class="s">{{ sub(track) }}</div>
      </div>
      <div class="trail">
        <KeyTag v-if="track.camelot" :camelot="track.camelot" :key-name="track.key" kind="tag" />
        <template v-if="track.bpm">{{ Math.round(track.bpm) }} BPM</template><br />
        {{ clock(track.duration_seconds) }}
      </div>
    </li>
  </ul>
</template>

<style scoped>
.list { list-style: none; margin: 0; padding: 0; }
.item { display: flex; align-items: center; gap: 16px; min-height: 72px; padding: 8px 0; }
.item + .item { border-top: 1px solid var(--md-sys-color-outline-variant); }
.leading {
  width: 40px; height: 40px; border-radius: var(--md-sys-shape-corner-full); flex: none; display: grid; place-items: center;
  background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container);
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
}
.item.forecast .leading { background: transparent; border: 1px dashed var(--md-sys-color-outline); color: var(--md-sys-color-on-surface-variant); }
/* Played tracks: a quieter badge, the text keeps its full contrast */
.item.past .leading { background: var(--md-sys-color-surface-container-high); color: var(--md-sys-color-on-surface-variant); }
.filled .item.past .leading { background: var(--md-sys-color-surface-container-lowest); }
.text { flex: 1; min-width: 0; }
.t {
  font: var(--md-sys-typescale-body-large);
  letter-spacing: var(--md-sys-typescale-body-large-tracking);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.s {
  font: var(--md-sys-typescale-body-medium);
  letter-spacing: var(--md-sys-typescale-body-medium-tracking);
  color: var(--md-sys-color-on-surface-variant); overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.trail {
  text-align: right; flex: none;
  font: var(--md-sys-typescale-label-medium);
  letter-spacing: var(--md-sys-typescale-label-medium-tracking);
  color: var(--md-sys-color-on-surface-variant);
}
</style>
