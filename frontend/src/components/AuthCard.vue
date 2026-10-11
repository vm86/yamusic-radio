<script setup lang="ts">
import { computed, reactive } from "vue";
import type { AuthInfo } from "../api";
import { t } from "../i18n";

const props = defineProps<{ auth: AuthInfo }>();
const emit = defineEmits<{
  select: [backend: string];
  signIn: [values: Record<string, string>];
}>();

// The service is chosen first, when there is a choice.
const backends = computed(() => props.auth.backends ?? []);
const chosen = computed(() => backends.value.length === 0 || Boolean(props.auth.backend));
const ready = computed(() => Boolean(props.auth.url && props.auth.code));
const values = reactive<Record<string, string>>({});
</script>

<template>
  <section class="card auth">
    <h2>{{ t("auth.title") }}</h2>

    <div v-if="backends.length" class="backends" role="group" :aria-label="t('auth.backends')">
      <button
        v-for="backend in backends"
        :key="backend.id"
        class="chip"
        type="button"
        :aria-pressed="backend.id === auth.backend"
        @click="emit('select', backend.id)"
      >
        {{ backend.name }}
      </button>
    </div>
    <p v-if="!chosen">{{ t("auth.choose") }}</p>

    <template v-else>
      <p v-if="auth.fields.length">{{ t("auth.fill") }}</p>
      <p v-else>{{ ready ? t("auth.open") : t("auth.waiting") }}</p>
      <div v-if="ready" class="code">{{ auth.code }}</div>
      <a v-if="ready" class="btn" :href="auth.url ?? undefined" target="_blank" rel="noopener">
        {{ t("auth.link") }}
      </a>
      <form v-if="auth.fields.length" @submit.prevent="emit('signIn', { ...values })">
        <label v-for="field in auth.fields" :key="field.name">
          <span>{{ field.label }}</span>
          <input v-model="values[field.name]" :type="field.secret ? 'password' : 'text'" :name="field.name" required />
        </label>
        <button class="btn" type="submit">{{ t("auth.submit") }}</button>
      </form>
    </template>
  </section>
</template>

<style scoped>
.auth { background: var(--md-sys-color-tertiary-container); color: var(--md-sys-color-on-tertiary-container); }
.auth h2 { color: inherit; }
.auth p { margin-top: 8px; }
.backends { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.code {
  margin: 16px 0; padding: 12px 16px; border-radius: var(--md-sys-shape-corner-small);
  background: color-mix(in srgb, currentColor 10%, transparent);
  font: var(--md-sys-typescale-headline-small); font-family: var(--md-ref-typeface-mono); font-weight: 500; letter-spacing: 4px; text-align: center;
  user-select: all;
}
form { display: grid; gap: 12px; margin-top: 16px; }
label { display: grid; gap: 4px; }
input {
  padding: 10px 12px; border-radius: var(--md-sys-shape-corner-small);
  border: 1px solid currentColor; background: transparent; color: inherit; font: inherit;
}
</style>
