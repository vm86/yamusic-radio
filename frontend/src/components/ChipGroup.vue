<script setup lang="ts">
import Icon from "./Icon.vue";

// Filter chips with "any" first: nothing selected means no restriction.
const props = defineProps<{
  label: string;
  anyLabel: string;
  options: string[];
  selected: string[];
  names?: Record<string, string>;
  disabled?: boolean;
}>();
const emit = defineEmits<{ change: [selected: string[]] }>();

function toggle(option: string): void {
  const next = new Set(props.selected);
  if (!next.delete(option)) next.add(option);
  emit("change", [...next]);
}
</script>

<template>
  <div class="chips" role="group" :aria-label="label">
    <button
      class="chip"
      type="button"
      :aria-pressed="!selected.length"
      :disabled="disabled"
      @click="emit('change', [])"
    >
      <Icon name="check" />{{ anyLabel }}
    </button>
    <button
      v-for="option in options"
      :key="option"
      class="chip"
      type="button"
      :aria-pressed="selected.includes(option)"
      :disabled="disabled"
      @click="toggle(option)"
    >
      <Icon name="check" />{{ names?.[option] ?? option }}
    </button>
  </div>
</template>
