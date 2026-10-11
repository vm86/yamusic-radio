export const POLL_MS = 3000;
// While the next track waits for a beat or is being blended, the button
// follows the radio closer than the usual poll does.
export const SKIP_POLL_MS = 1000;
// After a mood is picked the radio waits this long for another one, so that
// a few picks in a row make one change of the station's setting, not several.
export const MOODS_DELAY_MS = 1500;
