<script setup lang="ts">
import type { MoodsInfo } from "../api";
import { MOODS } from "../constants";
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
    <div class="card-head"><h2>Настроение микса</h2></div>
    <p class="hint" :class="{ pending }">
      {{
        pending
          ? "Выбор принят: ещё секунду подождём, вдруг выберете ещё настроение."
          : "Микс будет только между выбранными настроениями. Выбор меняет настройку станции в вашем аккаунте Яндекс Музыки."
      }}
    </p>
    <ChipGroup
      label="Настроение"
      any-label="Любое"
      :options="moods.available"
      :selected="selected"
      :names="MOODS"
      :disabled="disabled"
      @change="$emit('change', $event)"
    />
  </section>
</template>
