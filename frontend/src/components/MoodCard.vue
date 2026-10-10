<script setup lang="ts">
import type { MoodsInfo } from "../api";
import { moodNames, t } from "../i18n";
import ChipGroup from "./ChipGroup.vue";

defineProps<{
  moods: MoodsInfo;
  /** What the chips show: the picks that are waiting to be sent, or the radio's. */
  selected: string[];
  /** Picked, and the radio waits a little for another. */
  pending: boolean;
  disabled: boolean;
}>();
defineEmits<{ change: [moods: string[]] }>();
</script>

<template>
  <section class="card">
    <div class="card-head"><h2>{{ t("moods.title") }}</h2></div>
    <p class="hint" :class="{ pending }">
      {{
        pending
          ? t("moods.pending")
          : t("moods.hint")
      }}
    </p>
    <ChipGroup
      :label="t('moods.label')"
      :any-label="t('moods.any')"
      :options="moods.available"
      :selected="selected"
      :names="moodNames()"
      :disabled="disabled"
      @change="$emit('change', $event)"
    />
  </section>
</template>
