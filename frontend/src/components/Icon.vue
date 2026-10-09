<script setup lang="ts">
// The symbols live in SpriteSheet.vue, once per page.
defineProps<{ name: string }>();
</script>

<template>
  <svg class="icon" viewBox="0 0 24 24" aria-hidden="true"><use :href="`#i-${name}`" /></svg>
</template>
