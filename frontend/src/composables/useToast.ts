import { ref } from "vue";

const message = ref("");
const visible = ref(false);
let timer: ReturnType<typeof setTimeout> | undefined;

export function toast(text: string): void {
  message.value = text;
  visible.value = true;
  clearTimeout(timer);
  timer = setTimeout(() => (visible.value = false), 4000);
}

export function useToast() {
  return { message, visible };
}
