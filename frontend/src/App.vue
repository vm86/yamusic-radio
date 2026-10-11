<script setup lang="ts">
import AppBar from "./components/AppBar.vue";
import AuthCard from "./components/AuthCard.vue";
import GenreCard from "./components/GenreCard.vue";
import MoodCard from "./components/MoodCard.vue";
import PlayerCard from "./components/PlayerCard.vue";
import Snackbar from "./components/Snackbar.vue";
import SpriteSheet from "./components/SpriteSheet.vue";
import StatsCard from "./components/StatsCard.vue";
import TrackList from "./components/TrackList.vue";
import { usePlayer } from "./composables/usePlayer";
import { useRadio } from "./composables/useRadio";
import { t } from "./i18n";

const radio = useRadio();
const player = usePlayer(radio.now);
</script>

<template>
  <SpriteSheet />
  <AppBar :playing="player.playing.value" />

  <main>
    <AuthCard
      v-if="radio.auth.value && !radio.auth.value.authorized"
      :auth="radio.auth.value"
      class="auth-wide"
      @select="radio.selectBackend"
      @sign-in="radio.signIn"
    />

    <div class="column">
      <PlayerCard
        v-model:volume="player.volume.value"
        :now="radio.now.value"
        :elapsed="radio.elapsed.value"
        :playing="player.playing.value"
        :label="player.label.value"
        :can-skip="Boolean(radio.now.value && radio.auth.value?.authorized)"
        :skip-pending="radio.queue.value?.skip_pending ?? false"
        @toggle="player.toggle"
        @skip="radio.nextTrack"
      />
      <MoodCard
        v-if="radio.moods.value"
        :moods="radio.moods.value"
        :selected="radio.selectedMoods.value"
        :pending="radio.pendingMoods.value !== null && !radio.busy.value"
        :disabled="!radio.auth.value?.authorized"
        @change="radio.setMoods"
      />
      <GenreCard v-if="radio.genres.value" :genres="radio.genres.value" @change="radio.setGenres" @block="radio.setBlockedGenres" />
      <StatsCard v-if="radio.stats.value" :stats="radio.stats.value" />
    </div>

    <div class="column">
      <section class="card filled">
        <div class="card-head"><h2>{{ t("queue.played") }}</h2></div>
        <TrackList :tracks="radio.queue.value?.previous ?? []" :empty="t('queue.nothingYet')" kind="past" />
      </section>
      <section class="card">
        <div class="card-head">
          <h2>{{ t("queue.next") }}</h2>
          <button class="btn tonal" type="button" :disabled="radio.refreshing.value" @click="radio.refreshQueue">
            {{ t("queue.refresh") }}
          </button>
        </div>
        <p class="hint">{{ t("queue.forecastHint") }}</p>
        <TrackList :tracks="radio.queue.value?.next ?? []" :empty="t('queue.loading')" kind="queue" />
      </section>
    </div>
  </main>

  <Snackbar />
</template>

<style>
/* Layout: one column on compact windows, two from 600dp */
main {
  display: grid; gap: 16px;
  max-width: 1040px; margin: 0 auto; padding: 8px 16px calc(48px + env(safe-area-inset-bottom));
  grid-template-columns: minmax(0, 1fr);
}
.column { display: grid; grid-template-columns: minmax(0, 1fr); gap: 16px; align-content: start; }
@media (min-width: 600px) {
  main { grid-template-columns: minmax(0, 5fr) minmax(0, 6fr); align-items: start; }
  main > .auth-wide { grid-column: 1 / -1; }
}
@media (min-width: 840px) {
  main { gap: 24px; }
  .column { gap: 24px; }
}
</style>
