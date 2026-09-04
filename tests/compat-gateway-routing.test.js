import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const originalFetch = global.fetch;

function jsonResponse(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  };
}

describe("anthropic-compatible base URL composition", () => {
  it("appends /messages to a base that already ends in /v1", async () => {
    const { resolveAnthropicCompatMessagesUrl } = await import("open-sse/providers/shared.js");
    expect(resolveAnthropicCompatMessagesUrl("https://gateway.example.com/v1")).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("upgrades a bare origin to /v1 instead of producing origin/messages", async () => {
    const { resolveAnthropicCompatMessagesUrl } = await import("open-sse/providers/shared.js");
    expect(resolveAnthropicCompatMessagesUrl("https://gateway.example.com")).toBe(
      "https://gateway.example.com/v1/messages",
    );
    expect(resolveAnthropicCompatMessagesUrl("https://gateway.example.com/")).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("never doubles /v1 and tolerates a pasted /messages suffix", async () => {
    const { resolveAnthropicCompatMessagesUrl } = await import("open-sse/providers/shared.js");
    expect(resolveAnthropicCompatMessagesUrl("https://gateway.example.com/v1/messages")).toBe(
      "https://gateway.example.com/v1/messages",
    );
    expect(resolveAnthropicCompatMessagesUrl("https://gateway.example.com/v1/")).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("leaves a custom path base untouched (e.g. z.ai style /api/anthropic)", async () => {
    const { resolveAnthropicCompatMessagesUrl, resolveAnthropicCompatModelsUrl } = await import(
      "open-sse/providers/shared.js"
    );
    expect(resolveAnthropicCompatMessagesUrl("https://api.z.ai/api/anthropic/v1")).toBe(
      "https://api.z.ai/api/anthropic/v1/messages",
    );
    expect(resolveAnthropicCompatModelsUrl("https://api.z.ai/api/anthropic/v1")).toBe(
      "https://api.z.ai/api/anthropic/v1/models",
    );
  });

  it("falls back to the official Anthropic base when nothing is configured", async () => {
    const { resolveAnthropicCompatMessagesUrl, ANTHROPIC_COMPAT_BASE } = await import(
      "open-sse/providers/shared.js"
    );
    expect(resolveAnthropicCompatMessagesUrl("")).toBe(`${ANTHROPIC_COMPAT_BASE}/messages`);
    expect(resolveAnthropicCompatMessagesUrl(undefined)).toBe(`${ANTHROPIC_COMPAT_BASE}/messages`);
  });
});

describe("compat executors build the same endpoint as validation", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("DefaultExecutor targets <base>/messages for an anthropic-compatible node", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("anthropic-compatible-test");
    const credentials = { apiKey: "k", providerSpecificData: { baseUrl: "https://gateway.example.com/v1" } };

    expect(executor.buildUrl("some-model", true, 0, credentials)).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("DefaultExecutor upgrades a bare-origin anthropic-compatible base to /v1/messages", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("anthropic-compatible-test");
    const credentials = { apiKey: "k", providerSpecificData: { baseUrl: "https://gateway.example.com" } };

    expect(executor.buildUrl("some-model", true, 0, credentials)).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("BaseExecutor uses the same composition as DefaultExecutor", async () => {
    const { BaseExecutor } = await import("open-sse/executors/base.js");
    const executor = new BaseExecutor("anthropic-compatible-test", {});
    const credentials = { apiKey: "k", providerSpecificData: { baseUrl: "https://gateway.example.com" } };

    expect(executor.buildUrl("some-model", true, 0, credentials)).toBe(
      "https://gateway.example.com/v1/messages",
    );
  });

  it("sends both x-api-key and Bearer to third-party anthropic-compatible gateways", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("anthropic-compatible-test");
    const headers = executor.buildHeaders(
      { apiKey: "secret-key", providerSpecificData: { baseUrl: "https://gateway.example.com/v1" } },
      true,
    );

    expect(headers["x-api-key"]).toBe("secret-key");
    expect(headers["Authorization"]).toBe("Bearer secret-key");
    expect(headers["anthropic-version"]).toBeDefined();
  });
});

describe("compat executors forward the calling client's User-Agent", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("forwards the downstream UA to an anthropic-compatible gateway", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("anthropic-compatible-test");
    const headers = executor.buildHeaders(
      {
        apiKey: "k",
        providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
        rawHeaders: { "user-agent": "opencode/1.18.23" },
      },
      true,
    );

    expect(headers["User-Agent"]).toBe("opencode/1.18.23");
  });

  it("forwards the downstream UA to an openai-compatible gateway", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("openai-compatible-chat-test");
    const headers = executor.buildHeaders(
      {
        apiKey: "k",
        providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
        rawHeaders: { "User-Agent": "cline/3.0.60" },
      },
      true,
    );

    expect(headers["User-Agent"]).toBe("cline/3.0.60");
  });

  it("falls back to default agent User-Agent when the client sent none", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("openai-compatible-chat-test");
    const headers = executor.buildHeaders(
      { apiKey: "k", providerSpecificData: { baseUrl: "https://gateway.example.com/v1" }, rawHeaders: {} },
      true,
    );

    expect(headers["User-Agent"]).toBe("opencode/1.18.23");
  });

  it("leaves official api.anthropic.com traffic on the first-party fingerprint", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("anthropic-compatible-test");
    const headers = executor.buildHeaders(
      {
        apiKey: "k",
        providerSpecificData: { baseUrl: "https://api.anthropic.com/v1" },
        rawHeaders: { "user-agent": "opencode/1.18.23" },
      },
      true,
    );

    expect(headers["User-Agent"]).toBeUndefined();
  });
});

describe("provider-node Check separates inference from model discovery", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doMock("next/server", () => ({
      NextResponse: {
        json: (body, init = {}) => ({ status: init.status || 200, body }),
      },
    }));
    vi.doMock("@/dashboardGuard", () => ({ isLocalRequest: () => true }));
    vi.doMock("@/shared/utils/ssrfGuard.js", () => ({ assertPublicUrl: () => {} }));
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.doUnmock("next/server");
  });

  const makeRequest = (payload) => ({ json: () => Promise.resolve(payload) });

  it("validates via POST /messages when a Model ID is supplied and never calls /models", async () => {
    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, method: opts?.method });
      return Promise.resolve(jsonResponse(200, { content: [] }));
    });

    const { POST } = await import("@/app/api/provider-nodes/validate/route.js");
    const res = await POST(makeRequest({
      baseUrl: "https://gateway.example.com",
      apiKey: "k",
      type: "anthropic-compatible",
      modelId: "claude-test",
    }));

    expect(res.body).toMatchObject({ valid: true, method: "messages" });
    expect(calls).toHaveLength(1);
    expect(calls[0]).toEqual({ url: "https://gateway.example.com/v1/messages", method: "POST" });
  });

  it("reports Invalid with the real reason when /messages rejects the key", async () => {
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(401, { error: "bad key" })));

    const { POST } = await import("@/app/api/provider-nodes/validate/route.js");
    const res = await POST(makeRequest({
      baseUrl: "https://gateway.example.com/v1",
      apiKey: "k",
      type: "anthropic-compatible",
      modelId: "claude-test",
    }));

    expect(res.body).toMatchObject({ valid: false, method: "messages" });
    expect(res.body.error).toContain("unauthorized");
  });

  it("treats a 404 /messages as a Base URL problem, not a key problem", async () => {
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(404, {})));

    const { POST } = await import("@/app/api/provider-nodes/validate/route.js");
    const res = await POST(makeRequest({
      baseUrl: "https://gateway.example.com/v1",
      apiKey: "k",
      type: "anthropic-compatible",
      modelId: "claude-test",
    }));

    expect(res.body.valid).toBe(false);
    expect(res.body.error).toContain("Base URL");
  });

  it("counts 429 as authenticated-but-capacity-limited, not invalid key text", async () => {
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(429, {})));

    const { POST } = await import("@/app/api/provider-nodes/validate/route.js");
    const res = await POST(makeRequest({
      baseUrl: "https://gateway.example.com/v1",
      apiKey: "k",
      type: "anthropic-compatible",
      modelId: "claude-test",
    }));

    expect(res.body.error).toContain("Rate limited");
    expect(res.body.error).not.toContain("unauthorized");
  });

  it("falls back to /models only when no Model ID is given, and points at /messages on failure", async () => {
    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, method: opts?.method || "GET" });
      return Promise.resolve(jsonResponse(401, {}));
    });

    const { POST } = await import("@/app/api/provider-nodes/validate/route.js");
    const res = await POST(makeRequest({
      baseUrl: "https://gateway.example.com",
      apiKey: "k",
      type: "anthropic-compatible",
    }));

    expect(calls).toEqual([{ url: "https://gateway.example.com/v1/models", method: "GET" }]);
    expect(res.body.valid).toBe(false);
    expect(res.body.error).toContain("https://gateway.example.com/v1/messages");
  });
});

describe("Test Connection uses the stored connection key against the right endpoint", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  afterEach(() => {
    global.fetch = originalFetch;
  });

  async function loadTestUtils(connection) {
    vi.doMock("@/lib/localDb", () => ({
      getProviderConnectionById: () => Promise.resolve(connection),
      updateProviderConnection: () => Promise.resolve(connection),
    }));
    vi.doMock("@/lib/network/connectionProxy", () => ({
      resolveConnectionProxyConfig: () => Promise.resolve({}),
    }));
    vi.doMock("@/lib/network/proxyTest", () => ({ testProxyUrl: () => Promise.resolve({ ok: true }) }));
    return import("@/app/api/providers/[id]/test/testUtils.js");
  }

  it("POSTs the stored key to <base>/messages and accepts a 200", async () => {
    const connection = {
      id: "conn-1",
      provider: "anthropic-compatible-test",
      authType: "apikey",
      apiKey: "stored-key",
      defaultModel: "claude-test",
      providerSpecificData: { baseUrl: "https://gateway.example.com" },
    };
    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, method: opts?.method, headers: opts?.headers });
      return Promise.resolve(jsonResponse(200, {}));
    });

    const { testSingleConnection } = await loadTestUtils(connection);
    const result = await testSingleConnection("conn-1");

    expect(result.valid).toBe(true);
    expect(calls[0].url).toBe("https://gateway.example.com/v1/messages");
    expect(calls[0].headers["x-api-key"]).toBe("stored-key");
    expect(calls[0].headers["Authorization"]).toBe("Bearer stored-key");
  });

  it("no longer reports Valid when the endpoint 404s (the /v1/v1 false positive)", async () => {
    const connection = {
      id: "conn-2",
      provider: "anthropic-compatible-test",
      authType: "apikey",
      apiKey: "stored-key",
      defaultModel: "claude-test",
      providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
    };
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(404, {})));

    const { testSingleConnection } = await loadTestUtils(connection);
    const result = await testSingleConnection("conn-2");

    expect(result.valid).toBe(false);
    expect(result.error).toContain("404");
  });

  it("still counts 400 as an accepted key (model resolution error only)", async () => {
    const connection = {
      id: "conn-3",
      provider: "anthropic-compatible-test",
      authType: "apikey",
      apiKey: "stored-key",
      defaultModel: "claude-test",
      providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
    };
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(400, {})));

    const { testSingleConnection } = await loadTestUtils(connection);
    const result = await testSingleConnection("conn-3");

    expect(result.valid).toBe(true);
  });
});

describe("model discovery failures never claim the API key is invalid", () => {
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

  async function loadModelsRoute(connection) {
    vi.doMock("@/models", () => ({
      getProviderConnectionById: () => Promise.resolve(connection),
    }));
    return import("@/app/api/providers/[id]/models/route.js");
  }

  const params = { params: Promise.resolve({ id: "conn-1" }) };

  it("explains a 401 from /models as discovery-unavailable for anthropic-compatible", async () => {
    const connection = {
      id: "conn-1",
      provider: "anthropic-compatible-test",
      apiKey: "k",
      providerSpecificData: { baseUrl: "https://gateway.example.com" },
    };
    const calls = [];
    global.fetch = vi.fn((url) => {
      calls.push(url);
      return Promise.resolve(jsonResponse(401, {}));
    });

    const { GET } = await loadModelsRoute(connection);
    const res = await GET({}, params);

    expect(calls[0]).toBe("https://gateway.example.com/v1/models");
    expect(res.status).toBe(401);
    expect(res.body.error).toContain("Model discovery unavailable");
    expect(res.body.error).toContain("does not prove the API key is invalid");
  });

  it("explains a 404 from /models as no-discovery-endpoint for openai-compatible", async () => {
    const connection = {
      id: "conn-1",
      provider: "openai-compatible-chat-test",
      apiKey: "k",
      providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
    };
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(404, {})));

    const { GET } = await loadModelsRoute(connection);
    const res = await GET({}, params);

    expect(res.status).toBe(404);
    expect(res.body.error).toContain("no model discovery endpoint");
    expect(res.body.error).toContain("Add model IDs manually");
  });

  it("still returns the model list when discovery succeeds", async () => {
    const connection = {
      id: "conn-1",
      provider: "openai-compatible-chat-test",
      apiKey: "k",
      providerSpecificData: { baseUrl: "https://gateway.example.com/v1" },
    };
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(200, { data: [{ id: "m1" }] })));

    const { GET } = await loadModelsRoute(connection);
    const res = await GET({}, params);

    expect(res.body.models).toEqual([{ id: "m1" }]);
  });
});
