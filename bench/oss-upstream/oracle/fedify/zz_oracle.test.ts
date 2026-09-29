// Hidden oracle for fedify issue 1098: public mock behaviour only.
import { Note, Person } from "@fedify/vocab";
import { assert, assertEquals, assertStrictEquals } from "@std/assert";
import { createFederation } from "./mock.ts";

Deno.test("oracle: actor setters chain in the issue's order", () => {
  const f = createFederation<void>();
  f.setActorDispatcher("/users/{identifier}", () => null)
    .setKeyPairsDispatcher(() => [])
    .mapHandle(() => "alice");
});

Deno.test("oracle: every actor setter returns the setters object", () => {
  const f = createFederation<void>();
  // deno-lint-ignore no-explicit-any
  const s: any = f.setActorDispatcher("/users/{identifier}", () => null);
  assertStrictEquals(s.setKeyPairsDispatcher(() => []), s);
  assertStrictEquals(s.mapHandle(() => "alice"), s);
  assertStrictEquals(s.mapAlias(() => ({ identifier: "alice" })), s);
  assertStrictEquals(s.authorize(() => true), s);
  assertEquals(typeof s.mapActorAlias, "function");
  assertStrictEquals(s.mapActorAlias("/actor", "instance"), s);
});

Deno.test("oracle: object and collection setters chain authorize()", () => {
  const f = createFederation<void>();
  // deno-lint-ignore no-explicit-any
  const o: any = f.setObjectDispatcher(Note, "/notes/{id}", () => null);
  assertStrictEquals(o.authorize(() => true), o);
  // deno-lint-ignore no-explicit-any
  const c: any = f.setFollowersDispatcher("/users/{identifier}/followers", () => null);
  assertStrictEquals(c.setCounter(() => 0), c);
  assertStrictEquals(c.setFirstCursor(() => ""), c);
  assertStrictEquals(c.setLastCursor(() => ""), c);
  assertStrictEquals(c.authorize(() => true), c);
  // deno-lint-ignore no-explicit-any
  const out: any = f.setOutboxDispatcher("/users/{identifier}/outbox", () => null);
  assertStrictEquals(out.authorize(() => true), out);
});

Deno.test("oracle: chained actor setters keep the registered callbacks", async () => {
  const f = createFederation<void>();
  const kp = await crypto.subtle.generateKey(
    { name: "RSASSA-PKCS1-v1_5", modulusLength: 2048, publicExponent: new Uint8Array([1, 0, 1]), hash: "SHA-256" },
    true,
    ["sign", "verify"],
  );
  f.setActorDispatcher("/users/{identifier}", (ctx, id) => new Person({ id: ctx.getActorUri(id) }))
    .mapHandle(() => "alice")
    .setKeyPairsDispatcher((ctx, id) => [{ keyId: new URL(`${ctx.getActorUri(id).href}#k`), privateKey: kp.privateKey, publicKey: kp.publicKey }])
    .authorize(() => true);
  const ctx = f.createContext(new URL("https://example.com"), undefined);
  const pairs = await ctx.getActorKeyPairs("alice");
  assertEquals(pairs.length, 1);
  assert(pairs[0].keyId.href.endsWith("/users/alice#k"));
});
