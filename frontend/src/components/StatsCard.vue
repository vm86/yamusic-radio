<script setup lang="ts">
import { computed } from "vue";
import type { Stats } from "../api";
import { QUALITY, TRANSITIONS } from "../constants";

const props = defineProps<{ stats: Stats }>();

// Everything the radio has done, or this run if it keeps no history.
const data = computed(() => props.stats.all_time ?? props.stats.session);
const scope = computed(() => (props.stats.all_time ? "за всё время" : "с запуска"));

interface Row {
  key: string;
  name: string;
  count: number;
  share: number;
}

function rows(counts: Record<string, number>, names: Record<string, string>, total: number): Row[] {
  return Object.entries(counts).map(([key, count]) => ({
    key,
    name: names[key] ?? key,
    count,
    share: total ? (count / total) * 100 : 0,
  }));
}

const types = computed(() => rows(data.value.by_type, TRANSITIONS, data.value.total));
const rhythm = computed(() =>
  rows(
    { matched: data.value.beat_matched, free: data.value.total - data.value.beat_matched },
    { matched: "Биты совпали", free: "Без подгонки темпа" },
    data.value.total,
  ),
);
const quality = computed(() => {
  const { good, fair, poor } = data.value.by_quality;
  return rows({ good, fair, poor }, QUALITY, good + fair + poor);
});
const unknown = computed(() => data.value.by_quality.unknown);
</script>

<template>
  <section v-if="data.total" class="card">
    <div class="card-head">
      <h2>Переходы</h2>
      <span class="hint">{{ scope }}</span>
    </div>
    <template v-for="group in [
      { title: 'Тип', rows: types },
      { title: 'По ритму', rows: rhythm },
      { title: 'Удачность по тональностям', rows: quality },
    ]" :key="group.title">
      <h3>{{ group.title }}</h3>
      <div v-for="row in group.rows" :key="row.key" class="stat">
        <span>{{ row.name }}</span>
        <span class="n">{{ row.count }} · {{ Math.round(row.share) }}%</span>
        <div class="meter"><i :style="{ width: `${row.share}%` }" /></div>
      </div>
    </template>
    <p v-if="unknown" class="hint">Ещё {{ unknown }} — тональность неизвестна, не учтены.</p>
  </section>
</template>

<style scoped>
h3 { font: var(--md-sys-typescale-title-medium); letter-spacing: var(--md-sys-typescale-title-medium-tracking); margin: 16px 0 4px; }
h3:first-of-type { margin-top: 0; }
.stat { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; align-items: center; padding: 4px 0; }
.stat .n { font: var(--md-sys-typescale-label-large); color: var(--md-sys-color-on-surface-variant); }
.meter { grid-column: 1 / -1; height: 4px; border-radius: var(--md-sys-shape-corner-full); background: var(--md-sys-color-secondary-container); overflow: hidden; }
.meter > i { display: block; height: 100%; background: var(--md-sys-color-primary); }
</style>
