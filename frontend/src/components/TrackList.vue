<script setup lang="ts">
import type { TrackInfo } from "../api";
import { clock } from "../format";
import { moodNames, t } from "../i18n";
import CoverThumb from "./CoverThumb.vue";
import Icon from "./Icon.vue";
import KeyTag from "./KeyTag.vue";
import QueueMenu from "./QueueMenu.vue";

defineProps<{
  tracks: TrackInfo[];
  empty: string;
  kind: "past" | "queue";
  /** The first track of the queue is being blended in: it is fixed. */
  blending?: boolean;
}>();
// What is asked of a track of the queue; the position is where it is to be.
defineEmits<{ play: [id: string]; move: [id: string, position: number]; remove: [id: string] }>();

/** "−2", "−1" for what was played, "+1", "+2" for what comes. */
function badge(kind: "past" | "queue", index: number, count: number): string {
  return kind === "past" ? `−${count - index}` : `+${index + 1}`;
}

function sub(track: TrackInfo): string {
  return [track.artists.join(", "), moodNames()[track.mood]].filter(Boolean).join(" · ") || "—";
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
          <span v-if="kind === 'past'" class="sr">{{ t("track.playedBefore") }}</span>
          <span v-else-if="!track.decided" class="sr">{{ t("track.forecast") }}</span>
          <Icon v-if="track.pinned" name="pin" class="pin" />
          <span v-if="track.pinned" class="sr">{{ t("track.pinned") }}</span>
          · {{ sub(track) }}
        </div>
      </div>
      <div class="trail">
        <KeyTag v-if="track.camelot" :camelot="track.camelot" :key-name="track.key" kind="tag" />
        <template v-if="track.bpm">{{ Math.round(track.bpm) }} BPM</template><br />
        {{ clock(track.duration_seconds) }}
      </div>
      <QueueMenu
        v-if="kind === 'queue' && track.track_id"
        :title="track.title"
        :can-play="!blending"
        :can-up="index > (blending ? 1 : 0)"
        :can-down="index < tracks.length - 1 && !(blending && index === 0)"
        :can-remove="!(blending && index === 0)"
        @play="$emit('play', track.track_id)"
        @up="$emit('move', track.track_id, index - 1)"
        @down="$emit('move', track.track_id, index + 1)"
        @remove="$emit('remove', track.track_id)"
      />
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
.pin { width: 14px; height: 14px; vertical-align: -2px; color: var(--md-sys-color-primary); }
.pos { font-weight: 500; color: var(--md-sys-color-on-surface); }
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
