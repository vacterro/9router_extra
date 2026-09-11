import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import fs from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const originalFetch = global.fetch;

function jsonResponse(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  };
}

function makeNode(overrides = {}) {
  return {
    id: "anthropic-compatible-conn-1",
    type: "anthropic-compatible",
    name: "Test Node",
    baseUrl: "https://gateway.example.com/v1",
    ...overrides,
  };
}

describe("providers/validate — anthropic-compatible defaultModel precedence", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doMock("next/server", () => ({
      NextResponse: {
        json: (body, init = {}) => ({ status: init.status || 200, body }),
      },
    }));
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.doUnmock("next/server");
  });

  async function loadRoute(node) {
    vi.doMock("@/models", () => ({
      getProviderNodeById: () => Promise.resolve(node),
    }));
    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, method: opts?.method, headers: opts?.headers, body: opts?.body ? JSON.parse(opts.body) : null });
      return Promise.resolve(jsonResponse(200, {}));
    });
    const { POST } = await import("@/app/api/providers/validate/route.js");
    return { POST, calls };
  }

  const makeRequest = (payload) => ({ json: () => Promise.resolve(payload) });

  it("uses the request-supplied defaultModel when the node has none stored", async () => {
    const { POST, calls } = await loadRoute(makeNode());
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
      defaultModel: "gpt-5.6-sol",
    }));

    expect(res.body.valid).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://gateway.example.com/v1/messages");
    expect(calls[0].method).toBe("POST");
    expect(calls[0].body.model).toBe("gpt-5.6-sol");
    expect(calls[0].headers["x-api-key"]).toBe("k");
    expect(calls[0].headers["Authorization"]).toBe("Bearer k");
  });

  it("lets the request model override the stored node model", async () => {
    const { POST, calls } = await loadRoute(makeNode({ defaultModel: "stored-model" }));
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
      defaultModel: "requested-model",
    }));

    expect(res.body.valid).toBe(true);
    expect(calls[0].body.model).toBe("requested-model");
  });

  it("falls back to the stored node model when no request model is given", async () => {
    const { POST, calls } = await loadRoute(makeNode({ defaultModel: "stored-model" }));
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
    }));

    expect(res.body.valid).toBe(true);
    expect(calls[0].body.model).toBe("stored-model");
  });

  it("preserves the hardcoded fallback when neither model is available", async () => {
    const { POST, calls } = await loadRoute(makeNode());
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
    }));

    expect(res.body.valid).toBe(true);
    expect(calls[0].body.model).toBe("claude-3-haiku-20240307");
  });

  it("treats a whitespace-only defaultModel as absent", async () => {
    const { POST, calls } = await loadRoute(makeNode({ defaultModel: "stored-model" }));
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
      defaultModel: "   ",
    }));

    expect(res.body.valid).toBe(true);
    expect(calls[0].body.model).toBe("stored-model");
  });

  it("treats a malformed (non-string) defaultModel as absent without crashing", async () => {
    const { POST, calls } = await loadRoute(makeNode());
    const res = await POST(makeRequest({
      provider: "anthropic-compatible-conn-1",
      apiKey: "k",
      defaultModel: 123,
    }));

    expect(res.body.valid).toBe(true);
    expect(calls[0].body.model).toBe("claude-3-haiku-20240307");
  });

  it("never changes OpenAI-compatible validation behavior", async () => {
    const { POST, calls } = await loadRoute({
      id: "openai-compatible-chat-conn-1",
      type: "openai-compatible",
      name: "OpenAI Node",
      baseUrl: "https://gateway.example.com/v1",
    });
    const res = await POST(makeRequest({
      provider: "openai-compatible-chat-conn-1",
      apiKey: "k",
      defaultModel: "gpt-5.6-sol",
    }));

    expect(res.body.valid).toBe(true);
    // OpenAI-compatible validation still probes /models with Bearer auth and is
    // unaffected by a supplied defaultModel — never a /messages POST.
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://gateway.example.com/v1/models");
    expect(calls[0].method).toBeUndefined();
    expect(calls[0].headers["Authorization"]).toBe("Bearer k");
    expect(calls[0].headers["x-api-key"]).toBeUndefined();
  });
});

// The Add API Key dialog must forward the typed Default Model on every
// /api/providers/validate call (Check button and the save-time validation).
// The component lives in the engine source tree; skip the source scan when the
// file is not present (e.g. when this file is mirrored into another repo).
const modalPath = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../../src/app/(dashboard)/dashboard/providers/[id]/AddApiKeyModal.js",
);
const modalExists = fs.existsSync(modalPath);

describe.skipIf(!modalExists)("AddApiKeyModal validation payload includes typed Default Model", () => {
  it("forwards defaultModel through buildValidateBody on every /api/providers/validate call", () => {
    const source = fs.readFileSync(modalPath, "utf8");

    // The payload builder trims the typed Default Model into the body.
    expect(source).toMatch(/body\.defaultModel\s*=\s*formData\.defaultModel\.trim\(\)/);

    // Both validation call sites (Check button + save-time validation) use the
    // same builder so the model can never be dropped again.
    const validateCallCount = (source.match(/\/api\/providers\/validate/g) || []).length;
    const builderUseCount = (source.match(/JSON\.stringify\(buildValidateBody\(\)\)/g) || []).length;
    expect(validateCallCount).toBeGreaterThanOrEqual(2);
    expect(builderUseCount).toBe(2);
  });
});