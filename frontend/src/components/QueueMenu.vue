<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";
import { t } from "../i18n";
import Icon from "./Icon.vue";

// The menu of a track of the queue: an M3 menu that opens under its button.
defineProps<{
  title: string;
  canPlay: boolean;
  canUp: boolean;
  canDown: boolean;
  canRemove: boolean;
}>();
const emit = defineEmits<{ play: []; up: []; down: []; remove: [] }>();

const open = ref(false);
const root = ref<HTMLElement | null>(null);

function outside(event: Event): void {
  if (!root.value?.contains(event.target as Node)) open.value = false;
}
function escape(event: KeyboardEvent): void {
  if (event.key === "Escape") open.value = false;
}
function listen(on: boolean): void {
  const act = on ? document.addEventListener : document.removeEventListener;
  act.call(document, "click", outside);
  act.call(document, "keydown", escape as EventListener);
}
watch(open, listen);
onBeforeUnmount(() => listen(false));

function choose(action: "play" | "up" | "down" | "remove"): void {
  open.value = false;
  if (action === "play") emit("play");
  else if (action === "up") emit("up");
  else if (action === "down") emit("down");
  else emit("remove");
}
</script>

<template>
  <div ref="root" class="menu-root">
    <button
      class="icon-button"
      type="button"
      aria-haspopup="menu"
      :aria-expanded="open"
      :title="t('queue.menu', { title })"
      :aria-label="t('queue.menu', { title })"
      @click="open = !open"
    >
      <Icon name="more" />
    </button>
    <ul v-if="open" class="menu" role="menu">
      <li role="none">
        <button type="button" role="menuitem" :disabled="!canPlay" @click="choose('play')">
          <Icon name="play" />{{ t("queue.playNow") }}
        </button>
      </li>
      <li role="none">
        <button type="button" role="menuitem" :disabled="!canUp" @click="choose('up')">
          <Icon name="up" />{{ t("queue.up") }}
        </button>
      </li>
      <li role="none">
        <button type="button" role="menuitem" :disabled="!canDown" @click="choose('down')">
          <Icon name="down" />{{ t("queue.down") }}
        </button>
      </li>
      <li role="none">
        <button type="button" role="menuitem" :disabled="!canRemove" @click="choose('remove')">
          <Icon name="close" />{{ t("queue.remove") }}
        </button>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.menu-root { position: relative; flex: none; }
.menu {
  position: absolute; right: 0; top: 100%; z-index: 4; margin: 0; padding: 8px 0; list-style: none; min-width: 220px;
  background: var(--md-sys-color-surface-container); color: var(--md-sys-color-on-surface);
  border-radius: var(--md-sys-shape-corner-extra-small);
  /* A menu lies over the list: it keeps a shadow. */
  box-shadow: 0 3px 8px 3px var(--md-elevation-shadow);
}
.menu button {
  display: flex; align-items: center; gap: 12px; width: 100%; min-height: 48px; padding: 0 12px; border: 0;
  background: transparent; color: inherit; cursor: pointer; text-align: left;
  font: var(--md-sys-typescale-label-large);
  letter-spacing: var(--md-sys-typescale-label-large-tracking);
}
.menu button:hover:not(:disabled), .menu button:focus-visible { background: color-mix(in srgb, currentColor 8%, transparent); }
.menu button:disabled { opacity: 0.38; cursor: default; }
</style>
