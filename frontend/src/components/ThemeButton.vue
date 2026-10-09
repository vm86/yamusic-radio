<script setup lang="ts">
import { computed } from "vue";
import { useTheme, type ThemeMode } from "../composables/useTheme";
import Icon from "./Icon.vue";

// One icon button: each press goes system -> light -> dark and round again.
// The title is the tooltip and the accessible name, like the other apps' bar.
const TITLES: Record<ThemeMode, string> = {
  auto: "Тема: системная",
  light: "Тема: светлая",
  dark: "Тема: тёмная",
};

const { mode, cycle } = useTheme();
const title = computed(() => TITLES[mode.value]);
</script>

<template>
  <button class="icon-button" type="button" :title="title" :aria-label="title" @click="cycle">
    <Icon :name="mode" />
  </button>
</template>
