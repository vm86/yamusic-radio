<script setup lang="ts">
import { computed } from "vue";
import { t, useLocale } from "../i18n";

// Like the theme button: each press goes system -> Russian -> English.
const { mode, locale, cycle } = useLocale();
const title = computed(() => t(`locale.${mode.value}`));
</script>

<template>
  <button class="icon-button locale" type="button" :title="title" :aria-label="title" @click="cycle">
    <span aria-hidden="true">{{ locale }}</span>
    <i v-if="mode === 'auto'" class="auto" aria-hidden="true" />
  </button>
</template>

<style scoped>
.locale span {
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
  text-transform: uppercase;
}
/* A dot says that the language is the system's. */
.auto {
  position: absolute; top: 10px; right: 8px; width: 6px; height: 6px;
  border-radius: var(--md-sys-shape-corner-full); background: var(--md-sys-color-primary);
}
</style>
