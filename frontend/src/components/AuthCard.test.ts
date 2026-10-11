import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { AuthInfo, BackendInfo } from "../api";
import AuthCard from "./AuthCard.vue";

const backend = (id: string, over: Partial<BackendInfo> = {}): BackendInfo => ({
  id,
  name: id === "yandex" ? "Yandex Music" : "Local",
  enabled: false,
  authorized: false,
  available: false,
  url: null,
  code: null,
  fields: [],
  ...over,
});
const authOf = (backends: BackendInfo[], authorized = false): AuthInfo => ({
  authorized,
  backends,
  url: null,
  code: null,
  fields: [],
});

describe("AuthCard", () => {
  it("asks for the music sources while none is on", async () => {
    const wrapper = mount(AuthCard, { props: { auth: authOf([backend("yandex"), backend("local")]) } });

    expect(wrapper.text()).toContain("Выберите, откуда брать музыку");
    expect(wrapper.findAll(".chip")).toHaveLength(2);

    await wrapper.findAll(".chip")[1]!.trigger("click");
    expect(wrapper.emitted("toggle")).toEqual([["local", true]]);
  });

  it("turns a source off by pressing it again", async () => {
    const auth = authOf([backend("yandex", { enabled: true, authorized: true, available: true }), backend("local")], true);
    const wrapper = mount(AuthCard, { props: { auth } });

    const pressed = wrapper.findAll(".chip").map((c) => c.attributes("aria-pressed"));
    expect(pressed).toEqual(["true", "false"]);
    await wrapper.findAll(".chip")[0]!.trigger("click");

    expect(wrapper.emitted("toggle")).toEqual([["yandex", false]]);
    expect(wrapper.find(".sign-in").exists()).toBe(false);
  });

  it("shows the code of a source that waits for the sign-in", () => {
    const waiting = backend("yandex", { enabled: true, url: "https://ya.example", code: "ABCD" });
    const wrapper = mount(AuthCard, { props: { auth: authOf([waiting]) } });

    expect(wrapper.find(".code").text()).toBe("ABCD");
    expect(wrapper.find("a").attributes("href")).toBe("https://ya.example");
  });

  it("sends what is typed to the source that asked for it", async () => {
    const fields = [{ name: "password", label: "Пароль", secret: true }];
    const waiting = backend("local", { enabled: true, fields });
    const wrapper = mount(AuthCard, { props: { auth: authOf([backend("yandex"), waiting]) } });

    await wrapper.find("input").setValue("secret");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.find("input").attributes("type")).toBe("password");
    expect(wrapper.emitted("signIn")).toEqual([["local", { password: "secret" }]]);
  });

  it("says which source rests while the others play", () => {
    const rest = backend("yandex", { enabled: true, authorized: true, available: false });
    const wrapper = mount(AuthCard, { props: { auth: authOf([rest, backend("local")], true) } });

    expect(wrapper.text()).toContain("Yandex Music: не отвечает");
  });
});
