<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { coverUrl } from "../format";

// A round cover in a list; a plain disc when there is none (or it won't load).
const props = defineProps<{ cover: string }>();

const failed = ref(false);
watch(() => props.cover, () => (failed.value = false));
const src = computed(() => (failed.value ? "" : coverUrl(props.cover, 100)));
</script>

<template>
  <span class="thumb" aria-hidden="true">
    <img
      v-if="src"
      :src="src"
      alt=""
      loading="lazy"
      draggable="false"
      referrerpolicy="no-referrer"
      @error="failed = true"
    />
    <svg v-else class="icon" viewBox="0 0 100 100"><use href="#i-vinyl" /></svg>
  </span>
</template>

<style scoped>
.thumb {
  display: grid; place-items: center; width: 40px; height: 40px; flex: none; overflow: hidden;
  border-radius: var(--md-sys-shape-corner-full);
  background: var(--md-sys-color-primary-container); color: var(--md-sys-color-on-primary-container);
}
.thumb img { display: block; width: 100%; height: 100%; object-fit: cover; }
.thumb .icon { width: 100%; height: 100%; }
</style>
