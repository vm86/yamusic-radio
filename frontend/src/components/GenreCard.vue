<script setup lang="ts">
import { computed, ref } from "vue";
import type { GenresInfo } from "../api";
import { t } from "../i18n";
import ChipGroup from "./ChipGroup.vue";

const props = defineProps<{ genres: GenresInfo }>();
defineEmits<{ change: [genres: string[]]; block: [genres: string[]] }>();

// Picking genres changes nothing until the queue is rebuilt.
const pending = computed(
  () =>
    JSON.stringify(props.genres.selected) !== JSON.stringify(props.genres.applied) ||
    JSON.stringify(props.genres.blocked) !== JSON.stringify(props.genres.blocked_applied),
);
// Two tabs: the genres to play and the ones never to play.
type Tab = "play" | "blocked";
const tab = ref<Tab>("play");
const tabs = computed(() => [
  { id: "play" as const, label: t("genres.tabPlay"), count: props.genres.selected.length },
  { id: "blocked" as const, label: t("genres.blocked"), count: props.genres.blocked.length },
]);
const shown = computed(() => props.genres.available.length > 0 || props.genres.selected.length > 0);
</script>

<template>
  <section v-if="shown" class="card">
    <div class="card-head"><h2>{{ t("genres.title") }}</h2></div>
    <p class="hint" :class="{ pending }">
      {{ pending ? t("genres.pending") : t("genres.hint") }}
    </p>
    <div class="tabs" role="tablist">
      <button
        v-for="item in tabs"
        :id="`genres-tab-${item.id}`"
        :key="item.id"
        class="tab"
        type="button"
        role="tab"
        :aria-selected="tab === item.id"
        aria-controls="genres-panel"
        @click="tab = item.id"
      >
        {{ item.label }}<span v-if="item.count" class="count">{{ item.count }}</span>
      </button>
    </div>
    <div id="genres-panel" role="tabpanel" :aria-labelledby="`genres-tab-${tab}`">
      <ChipGroup
        v-if="tab === 'play'"
        :label="t('genres.tabPlay')"
        :any-label="t('genres.any')"
        :options="genres.available"
        :selected="genres.selected"
        @change="$emit('change', $event)"
      />
      <ChipGroup
        v-else
        :label="t('genres.blocked')"
        :any-label="t('genres.blockedNone')"
        :options="genres.available"
        :selected="genres.blocked"
        @change="$emit('block', $event)"
      />
    </div>
  </section>
</template>

<style scoped>
/* M3 secondary tabs */
.tabs { display: flex; border-bottom: 1px solid var(--md-sys-color-outline-variant); margin-bottom: 16px; }
.tab {
  position: relative; flex: 1; min-height: 48px; border: 0; background: transparent; cursor: pointer;
  display: inline-flex; align-items: center; justify-content: center; gap: 8px;
  color: var(--md-sys-color-on-surface-variant);
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
}
.tab[aria-selected="true"] { color: var(--md-sys-color-primary); }
.tab[aria-selected="true"]::before {
  content: ""; position: absolute; left: 0; right: 0; bottom: -1px; height: 2px;
  background: var(--md-sys-color-primary);
}
.tab:hover { background: color-mix(in srgb, currentColor 8%, transparent); }
.count {
  min-width: 20px; height: 20px; padding: 0 6px; border-radius: var(--md-sys-shape-corner-full);
  display: inline-grid; place-items: center;
  background: var(--md-sys-color-secondary-container); color: var(--md-sys-color-on-secondary-container);
  font: var(--md-sys-typescale-label-medium);
}
</style>
