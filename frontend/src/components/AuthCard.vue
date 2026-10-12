<script setup lang="ts">
import { computed, reactive } from "vue";
import type { AuthField, AuthInfo, BackendInfo } from "../api";
import { t, tryT } from "../i18n";

const props = defineProps<{ auth: AuthInfo }>();
const emit = defineEmits<{
  toggle: [backend: string, on: boolean];
  signIn: [backend: string, values: Record<string, string>];
}>();

const backends = computed(() => props.auth.backends ?? []);
// A service that is on and not signed in yet shows how to sign in.
const waiting = computed(() => backends.value.filter((b) => b.enabled && !b.authorized));
const resting = computed(() => backends.value.filter((b) => b.authorized && !b.available));
const values = reactive<Record<string, Record<string, string>>>({});
// A source that is signed in and has settings (the folder) lets them be changed.
const settingFields = (backend: BackendInfo): AuthField[] => backend.setting_fields ?? [];
const configurable = computed(() => backends.value.filter((b) => b.enabled && b.authorized && settingFields(b).length));
const edits = reactive<Record<string, Record<string, string>>>({});

function edited(backend: BackendInfo): Record<string, string> {
  return (edits[backend.id] ??= { ...(backend.settings ?? {}) });
}
function fieldLabel(field: AuthField): string {
  return tryT(`field.${field.name}`, field.label);
}

function ready(backend: BackendInfo): boolean {
  return Boolean(backend.url && backend.code);
}
function typed(backend: BackendInfo): Record<string, string> {
  return (values[backend.id] ??= {});
}
</script>

<template>
  <section class="card auth">
    <h2>{{ auth.authorized ? t("auth.titleSources") : t("auth.title") }}</h2>

    <div v-if="backends.length" class="backends" role="group" :aria-label="t('auth.backends')">
      <button
        v-for="backend in backends"
        :key="backend.id"
        class="chip"
        type="button"
        :aria-pressed="backend.enabled"
        @click="emit('toggle', backend.id, !backend.enabled)"
      >
        {{ backend.name }}
      </button>
    </div>
    <p v-if="!backends.some((b) => b.enabled) && backends.length">{{ t("auth.choose") }}</p>
    <p v-for="backend in resting" :key="backend.id" class="note">{{ backend.name }}: {{ t("auth.resting") }}</p>

    <div v-for="backend in waiting" :key="backend.id" class="sign-in">
      <h3>{{ backend.name }}</h3>
      <p v-if="settingFields(backend).length">{{ t("auth.setup") }}</p>
      <p v-else-if="backend.fields.length">{{ t("auth.fill") }}</p>
      <p v-else>{{ ready(backend) ? t("auth.open") : t("auth.waiting") }}</p>
      <div v-if="ready(backend)" class="code">{{ backend.code }}</div>
      <a v-if="ready(backend)" class="btn" :href="backend.url ?? undefined" target="_blank" rel="noopener">
        {{ t("auth.link") }}
      </a>
      <form v-if="backend.fields.length" @submit.prevent="emit('signIn', backend.id, { ...typed(backend) })">
        <label v-for="field in backend.fields" :key="field.name">
          <span>{{ fieldLabel(field) }}</span>
          <input
            v-model="typed(backend)[field.name]"
            :type="field.secret ? 'password' : 'text'"
            :name="field.name"
            required
          />
        </label>
        <button class="btn" type="submit">{{ settingFields(backend).length ? t("auth.set") : t("auth.submit") }}</button>
      </form>
    </div>

    <div v-for="backend in configurable" :key="backend.id" class="sign-in">
      <h3>{{ backend.name }}</h3>
      <form @submit.prevent="emit('signIn', backend.id, { ...edited(backend) })">
        <label v-for="field in settingFields(backend)" :key="field.name">
          <span>{{ fieldLabel(field) }}</span>
          <input v-model="edited(backend)[field.name]" type="text" :name="field.name" required />
        </label>
        <button class="btn" type="submit">{{ t("auth.change") }}</button>
      </form>
    </div>
  </section>
</template>

<style scoped>
.auth { background: var(--md-sys-color-tertiary-container); color: var(--md-sys-color-on-tertiary-container); }
.auth h2, .auth h3 { color: inherit; }
.auth p { margin-top: 8px; }
.backends { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.sign-in { margin-top: 16px; }
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
