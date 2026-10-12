<script setup lang="ts">
import { computed } from "vue";
import type { LanguageInfo } from "../api";
import { languageNames, t } from "../i18n";
import ChipGroup from "./ChipGroup.vue";

const props = defineProps<{ language: LanguageInfo; disabled?: boolean }>();
const emit = defineEmits<{ change: [language: string | null] }>();

// What the station is set to; "any" is the chip of no pick.
const shown = computed(() => {
  const now = props.language.selected ?? props.language.station;
  return now && now !== "any" ? [now] : [];
});
const options = computed(() => props.language.available.filter((language) => language !== "any"));

// One language at a time: the chip that was just pressed replaces the one before.
function pick(list: string[]): void {
  const now = shown.value[0];
  const next = list.find((language) => language !== now);
  // "Any" is a pick of its own: the station is set to any language.
  emit("change", next ?? "any");
}
</script>

<template>
  <section class="card">
    <div class="card-head"><h2>{{ t("language.title") }}</h2></div>
    <p class="hint">{{ t("language.hint") }}</p>
    <ChipGroup
      :label="t('language.title')"
      :any-label="t('language.any')"
      :options="options"
      :selected="shown"
      :names="languageNames()"
      :disabled="disabled"
      @change="pick"
    />
  </section>
</template>
