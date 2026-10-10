<script setup lang="ts">
import { computed } from "vue";
import { useTheme } from "../composables/useTheme";
import { t } from "../i18n";
import Icon from "./Icon.vue";

// One icon button: each press goes system -> light -> dark and round again.
// The title is the tooltip and the accessible name, like the other apps' bar.
const { mode, cycle } = useTheme();
const title = computed(() => t(`theme.${mode.value}`));
</script>

<template>
  <button class="icon-button" type="button" :title="title" :aria-label="title" @click="cycle">
    <Icon :name="mode" />
  </button>
</template>
