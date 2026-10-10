<script setup lang="ts">
import { computed } from "vue";
import type { AuthInfo } from "../api";
import { t } from "../i18n";

const props = defineProps<{ auth: AuthInfo }>();
const ready = computed(() => Boolean(props.auth.url && props.auth.code));
</script>

<template>
  <section class="card auth">
    <h2>{{ t("auth.title") }}</h2>
    <p>
      {{
        ready
          ? t("auth.open")
          : t("auth.waiting")
      }}
    </p>
    <div v-if="ready" class="code">{{ auth.code }}</div>
    <a v-if="ready" class="btn" :href="auth.url ?? undefined" target="_blank" rel="noopener">
      {{ t("auth.link") }}
    </a>
  </section>
</template>

<style scoped>
.auth { background: var(--md-sys-color-tertiary-container); color: var(--md-sys-color-on-tertiary-container); }
.auth h2 { color: inherit; }
.auth p { margin-top: 8px; }
.code {
  margin: 16px 0; padding: 12px 16px; border-radius: var(--md-sys-shape-corner-small);
  background: color-mix(in srgb, currentColor 10%, transparent);
  font: 500 28px/36px var(--md-ref-typeface-mono); letter-spacing: 4px; text-align: center;
  user-select: all;
}
</style>
