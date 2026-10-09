<script setup lang="ts">
import type { TrackInfo } from "../api";
import { MOODS } from "../constants";
import { clock } from "../format";
import CoverThumb from "./CoverThumb.vue";
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
      <CoverThumb :cover="track.cover" class="cover" />
      <div class="text">
        <div class="t" :title="track.title">{{ track.title }}</div>
        <div class="s">
          <span class="pos">{{ badge(kind, index, tracks.length) }}</span>
          <!-- The ring round the cover says it to the eye, this to a screen reader. -->
          <span v-if="kind === 'past'" class="sr">, уже играл</span>
          <span v-else-if="!track.decided" class="sr">, прогноз</span>
          · {{ sub(track) }}
        </div>
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
/* The ring round the cover: solid for the next one, dashed for a forecast */
.item.ready .cover { outline: 2px solid var(--md-sys-color-primary); outline-offset: 2px; }
.item.forecast .cover { outline: 1px dashed var(--md-sys-color-outline); outline-offset: 2px; }
.item.past .cover { filter: saturate(0.5); }
.pos { font-weight: 600; color: var(--md-sys-color-on-surface); }
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
