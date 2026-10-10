<script setup lang="ts">
import { computed } from "vue";
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
const shown = computed(() => props.genres.available.length > 0 || props.genres.selected.length > 0);
</script>

<template>
  <section v-if="shown" class="card">
    <div class="card-head"><h2>{{ t("genres.title") }}</h2></div>
    <p class="hint" :class="{ pending }">
      {{
        pending
          ? t("genres.pending")
          : t("genres.hint")
      }}
    </p>
    <ChipGroup
      :label="t('genres.title')"
      :any-label="t('genres.any')"
      :options="genres.available"
      :selected="genres.selected"
      @change="$emit('change', $event)"
    />
    <h3>{{ t("genres.blocked") }}</h3>
    <ChipGroup
      :label="t('genres.blocked')"
      :any-label="t('genres.blockedNone')"
      :options="genres.available"
      :selected="genres.blocked"
      @change="$emit('block', $event)"
    />
  </section>
</template>

<style scoped>
h3 {
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
  color: var(--md-sys-color-on-surface-variant);
  margin: 16px 0 8px;
}
</style>
