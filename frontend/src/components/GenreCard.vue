<script setup lang="ts">
import { computed } from "vue";
import type { GenresInfo } from "../api";
import ChipGroup from "./ChipGroup.vue";

const props = defineProps<{ genres: GenresInfo }>();
defineEmits<{ change: [genres: string[]] }>();

// Picking genres changes nothing until the queue is rebuilt.
const pending = computed(
  () => JSON.stringify(props.genres.selected) !== JSON.stringify(props.genres.applied),
);
const shown = computed(() => props.genres.available.length > 0 || props.genres.selected.length > 0);
</script>

<template>
  <section v-if="shown" class="card">
    <div class="card-head"><h2>Жанры</h2></div>
    <p class="hint" :class="{ pending }">
      {{
        pending
          ? "Выбор изменён: он вступит в силу после кнопки «Обновить очередь»."
          : "Очередь строится из выбранных жанров, пока выбор не изменится. Применяется кнопкой «Обновить очередь». Жанры берутся из уже проигранных треков."
      }}
    </p>
    <ChipGroup
      label="Жанры"
      any-label="Любые"
      :options="genres.available"
      :selected="genres.selected"
      @change="$emit('change', $event)"
    />
  </section>
</template>
