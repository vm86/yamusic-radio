export function clock(seconds: number | undefined): string {
  const total = Math.max(0, Math.round(seconds ?? 0));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

/** The hue of a Camelot code (1A..12B): the 12 numbers go round the wheel. */
export function camelotHue(camelot: string): number {
  const number = Number.parseInt(camelot, 10);
  return Number.isNaN(number) ? 0 : ((number - 1) * 30) % 360;
}

/** The cover in a size Yandex serves ("100x100", "400x400"...), or "". */
export function coverUrl(template: string, size: number): string {
  return template ? template.replace("%%", `${size}x${size}`) : "";
}
