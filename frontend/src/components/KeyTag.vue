<script setup lang="ts">
import { computed } from "vue";
import { camelotHue } from "../format";

// The hue marks the place on the Camelot wheel (a decorative dot, the code
// says it all), so neighbours on the wheel look alike.
const props = defineProps<{ camelot: string; keyName: string; kind: "assist" | "tag" }>();
const hue = computed(() => camelotHue(props.camelot));
</script>

<template>
  <span
    :class="[kind, 'key']"
    :title="`${keyName} (Camelot ${camelot})`"
    :style="{ '--key-hue': hue }"
  >
    {{ camelot }}<span class="sr">, {{ keyName }}</span>
  </span>
</template>
