import { mount } from "@vue/test-utils";
import { describe, expect, it } from "vitest";
import type { AuthInfo } from "../api";
import AuthCard from "./AuthCard.vue";

const base: AuthInfo = { authorized: false, url: null, code: null, fields: [] };
const backends = [
  { id: "yandex", name: "Yandex Music" },
  { id: "local", name: "Local" },
];

describe("AuthCard", () => {
  it("asks for the music service while none is chosen", async () => {
    const wrapper = mount(AuthCard, { props: { auth: { ...base, backend: null, backends } } });

    expect(wrapper.text()).toContain("Выберите, откуда брать музыку");
    expect(wrapper.findAll(".chip")).toHaveLength(2);
    expect(wrapper.find(".code").exists()).toBe(false);

    await wrapper.findAll(".chip")[1]!.trigger("click");
    expect(wrapper.emitted("select")).toEqual([["local"]]);
  });

  it("shows the chosen service and its code", () => {
    const auth = { ...base, backend: "yandex", backends, url: "https://ya.example", code: "ABCD" };
    const wrapper = mount(AuthCard, { props: { auth } });

    const pressed = wrapper.findAll(".chip").map((c) => c.attributes("aria-pressed"));
    expect(pressed).toEqual(["true", "false"]);
    expect(wrapper.find(".code").text()).toBe("ABCD");
    expect(wrapper.find("a").attributes("href")).toBe("https://ya.example");
  });

  it("sends what is typed for a backend that asks for a login", async () => {
    const fields = [{ name: "password", label: "Пароль", secret: true }];
    const wrapper = mount(AuthCard, { props: { auth: { ...base, backend: "local", backends, fields } } });

    await wrapper.find("input").setValue("secret");
    await wrapper.find("form").trigger("submit");

    expect(wrapper.find("input").attributes("type")).toBe("password");
    expect(wrapper.emitted("signIn")).toEqual([[{ password: "secret" }]]);
  });

  it("has no picker when the radio offers no choice", () => {
    const wrapper = mount(AuthCard, { props: { auth: base } });

    expect(wrapper.find(".backends").exists()).toBe(false);
    expect(wrapper.text()).toContain("Ждём код");
  });
});
