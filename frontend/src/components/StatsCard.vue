<script setup lang="ts">
import { computed } from "vue";
import type { Stats } from "../api";
import { effectNames, qualityNames, t, transitionNames } from "../i18n";

const props = defineProps<{ stats: Stats }>();

// Everything the radio has done, or this run if it keeps no history.
const data = computed(() => props.stats.all_time ?? props.stats.session);
const scope = computed(() => (props.stats.all_time ? t("stats.scopeAll") : t("stats.scopeRun")));

interface Row {
  key: string;
  name: string;
  count: number;
  share: number;
  /** Left out of the shares above it: shown dimmer. */
  muted?: boolean;
}

function rows(counts: Record<string, number>, names: Record<string, string>, total: number): Row[] {
  return Object.entries(counts).map(([key, count]) => ({
    key,
    name: names[key] ?? key,
    count,
    share: total ? (count / total) * 100 : 0,
  }));
}

const types = computed(() => rows(data.value.by_type, transitionNames(), data.value.total));
const rhythm = computed(() =>
  rows(
    { matched: data.value.beat_matched, free: data.value.total - data.value.beat_matched },
    { matched: t("stats.matched"), free: t("stats.free") },
    data.value.total,
  ),
);
// Blends made before the effects were counted can't be told apart: they are
// not a row, and not counted in the shares.
const effects = computed(() => {
  const { unknown: _unknown, ...known } = data.value.by_effect;
  const total = Object.values(known).reduce((sum, count) => sum + count, 0);
  return rows(known, effectNames(), total);
});
// The blends whose keys are not known are left out of the shares of the
// quality: a bar of their own says how many there are, of all the blends.
const quality = computed(() => {
  const { good, fair, poor, unknown } = data.value.by_quality;
  const found = rows({ good, fair, poor }, qualityNames(), good + fair + poor);
  if (!unknown || !data.value.total) return found;
  return [
    ...found,
    {
      key: "unknown",
      name: t("stats.unknownKey"),
      count: unknown,
      share: (unknown / data.value.total) * 100,
      muted: true,
    },
  ];
});
</script>

<template>
  <section v-if="data.total" class="card">
    <div class="card-head">
      <h2>{{ t("stats.title") }}</h2>
      <span class="hint">{{ scope }}</span>
    </div>
    <template v-for="group in [
      { title: t('stats.type'), rows: types },
      { title: t('stats.effects'), rows: effects },
      { title: t('stats.rhythm'), rows: rhythm },
      { title: t('stats.quality'), rows: quality },
    ]" :key="group.title">
      <h3>{{ group.title }}</h3>
      <div v-for="row in group.rows" :key="row.key" class="stat">
        <span>{{ row.name }}</span>
        <span class="n">{{ row.count }} · {{ Math.round(row.share) }}%</span>
        <div class="meter" :class="{ muted: row.muted }"><i :style="{ width: `${row.share}%` }" /></div>
      </div>
    </template>
  </section>
</template>

<style scoped>
h3 { font: var(--md-sys-typescale-title-medium); letter-spacing: var(--md-sys-typescale-title-medium-tracking); margin: 16px 0 4px; }
h3:first-of-type { margin-top: 0; }
.stat { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 12px; align-items: center; padding: 4px 0; }
.stat .n { font: var(--md-sys-typescale-label-large); color: var(--md-sys-color-on-surface-variant); }
.meter { grid-column: 1 / -1; height: 4px; border-radius: var(--md-sys-shape-corner-full); background: var(--md-sys-color-secondary-container); overflow: hidden; }
.meter > i { display: block; height: 100%; background: var(--md-sys-color-primary); }
.meter.muted > i { background: var(--md-sys-color-outline); }
.stat:has(.meter.muted) { color: var(--md-sys-color-on-surface-variant); }
</style>
