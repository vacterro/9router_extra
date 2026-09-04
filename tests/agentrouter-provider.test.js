import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const originalFetch = global.fetch;

function jsonResponse(status, body = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
    headers: { get: () => "application/json" },
  };
}

describe("agentrouter registry entry", () => {
  beforeEach(() => {
    vi.resetModules();
  });

  it("is registered and exposes both wire formats on the same /v1 base", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    const cfg = PROVIDERS.agentrouter;

    expect(cfg).toBeDefined();
    const byFormat = Object.fromEntries(cfg.transports.map((t) => [t.format, t.baseUrl]));
    expect(byFormat.openai).toBe("https://agentrouter.org/v1/chat/completions");
    expect(byFormat.claude).toBe("https://agentrouter.org/v1/messages");
  });

  it("defaults to Chat Completions so models without Messages support still route", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    expect(PROVIDERS.agentrouter.baseUrl).toBe("https://agentrouter.org/v1/chat/completions");
    expect(PROVIDERS.agentrouter.format).toBe("openai");
  });

  it("validates against the ungated account endpoint, not a gated inference endpoint", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    const validateUrl = PROVIDERS.agentrouter.validateUrl;

    expect(validateUrl).toBe("https://agentrouter.org/v1/dashboard/billing/subscription");
    expect(validateUrl).not.toContain("/chat/completions");
    expect(validateUrl).not.toContain("/messages");
    expect(validateUrl).not.toContain("/models");
  });

  it("declares per-model supportedFormats matching the upstream endpoint types", async () => {
    const { getModelsByProviderId } = await import("open-sse/config/providerModels.js");
    const models = Object.fromEntries(getModelsByProviderId("agentrouter").map((m) => [m.id, m.supportedFormats]));

    expect(models["gpt-5.6-sol"]).toEqual(["openai"]);
    expect(models["claude-opus-4-8"]).toEqual(["openai", "claude"]);
    expect(models["deepseek-v4-flash"]).toEqual(["openai", "claude"]);
  });

  it("uses Bearer auth on both transports (never a bare x-api-key)", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    for (const t of PROVIDERS.agentrouter.transports) {
      expect(t.auth.header).toBe("Authorization");
      expect(t.auth.scheme).toBe("bearer");
    }
  });

  it("forwards the calling client's User-Agent on both transports", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("agentrouter");

    for (const rt of PROVIDERS.agentrouter.transports) {
      expect(rt.auth.hooks).toContain("forwardClientUserAgent");
      const headers = executor.buildHeaders(
        { apiKey: "secret", runtimeTransport: rt, rawHeaders: { "user-agent": "opencode/1.18.23" } },
        true,
      );
      expect(headers["User-Agent"]).toBe("opencode/1.18.23");
      expect(headers["Authorization"]).toBe("Bearer secret");
    }
  });

  it("falls back to default agent User-Agent when the client provided none", async () => {
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("agentrouter");
    const headers = executor.buildHeaders({ apiKey: "secret", rawHeaders: {} }, true);

    expect(headers["User-Agent"]).toBe("opencode/1.18.23");
  });

  it("adds anthropic-version only on the Messages transport", async () => {
    const { PROVIDERS } = await import("open-sse/providers/index.js");
    const { DefaultExecutor } = await import("open-sse/executors/default.js");
    const executor = new DefaultExecutor("agentrouter");
    const byFormat = Object.fromEntries(PROVIDERS.agentrouter.transports.map((t) => [t.format, t]));

    const claudeHeaders = executor.buildHeaders({ apiKey: "k", runtimeTransport: byFormat.claude }, true);
    const openaiHeaders = executor.buildHeaders({ apiKey: "k", runtimeTransport: byFormat.openai }, true);

    expect(claudeHeaders["anthropic-version"]).toBe("2023-06-01");
    expect(openaiHeaders["anthropic-version"]).toBeUndefined();
  });

  it("discovers models from the ungated pricing feed", async () => {
    const { PROVIDER_MEDIA } = await import("open-sse/providers/index.js");
    expect(PROVIDER_MEDIA.agentrouter.modelsFetcher).toEqual({
      url: "https://agentrouter.org/api/pricing",
      type: "agentrouter",
    });
  });

  it("maps pricing-feed rows to model ids", async () => {
    const { FILTERS } = await import("@/app/api/providers/suggested-models/filters.js");
    const rows = [
      { model_name: "deepseek-v4-flash", supported_endpoint_types: ["openai", "anthropic"] },
      { model_name: "gpt-5.6-sol", supported_endpoint_types: ["openai"] },
      { nonsense: true },
    ];

    expect(FILTERS.agentrouter(rows)).toEqual([
      { id: "deepseek-v4-flash", name: "deepseek-v4-flash" },
      { id: "gpt-5.6-sol", name: "gpt-5.6-sol" },
    ]);
  });
});

describe("agentrouter dashboard validation", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.doMock("next/server", () => ({
      NextResponse: { json: (body, init = {}) => ({ status: init.status || 200, body }) },
    }));
  });

  afterEach(() => {
    global.fetch = originalFetch;
    vi.doUnmock("next/server");
  });

  it("Check probes the account endpoint with Bearer and accepts a live key", async () => {
    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, headers: opts?.headers, method: opts?.method || "GET" });
      return Promise.resolve(jsonResponse(200, { object: "billing_subscription" }));
    });

    const { POST } = await import("@/app/api/providers/validate/route.js");
    const res = await POST({ json: () => Promise.resolve({ provider: "agentrouter", apiKey: "live-key" }) });

    expect(res.body).toEqual({ valid: true, error: null });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("https://agentrouter.org/v1/dashboard/billing/subscription");
    expect(calls[0].method).toBe("GET");
    expect(calls[0].headers.Authorization).toBe("Bearer live-key");
  });

  it("Check reports invalid on a 401 from the account endpoint", async () => {
    global.fetch = vi.fn(() => Promise.resolve(jsonResponse(401, {})));

    const { POST } = await import("@/app/api/providers/validate/route.js");
    const res = await POST({ json: () => Promise.resolve({ provider: "agentrouter", apiKey: "dead-key" }) });

    expect(res.body).toEqual({ valid: false, error: "Invalid API key" });
  });

  it("Test Connection uses the stored connection key against the same endpoint", async () => {
    const connection = {
      id: "ar-1",
      provider: "agentrouter",
      authType: "apikey",
      apiKey: "stored-key",
      defaultModel: "deepseek-v4-flash",
      providerSpecificData: {},
    };
    vi.doMock("@/lib/localDb", () => ({
      getProviderConnectionById: () => Promise.resolve(connection),
      updateProviderConnection: () => Promise.resolve(connection),
    }));
    vi.doMock("@/lib/network/connectionProxy", () => ({ resolveConnectionProxyConfig: () => Promise.resolve({}) }));
    vi.doMock("@/lib/network/proxyTest", () => ({ testProxyUrl: () => Promise.resolve({ ok: true }) }));

    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, headers: opts?.headers });
      return Promise.resolve(jsonResponse(200, {}));
    });

    const { testSingleConnection } = await import("@/app/api/providers/[id]/test/testUtils.js");
    const result = await testSingleConnection("ar-1");

    expect(result.valid).toBe(true);
    expect(calls[0].url).toBe("https://agentrouter.org/v1/dashboard/billing/subscription");
    expect(calls[0].headers.Authorization).toBe("Bearer stored-key");
  });

  it("Import from /models reads the pricing feed with no credentials attached", async () => {
    const connection = { id: "ar-1", provider: "agentrouter", apiKey: "stored-key", providerSpecificData: {} };
    vi.doMock("@/models", () => ({ getProviderConnectionById: () => Promise.resolve(connection) }));

    const calls = [];
    global.fetch = vi.fn((url, opts) => {
      calls.push({ url, headers: opts?.headers });
      return Promise.resolve(jsonResponse(200, { data: [{ model_name: "glm-5.3" }, { model_name: "claude-opus-5" }] }));
    });

    const { GET } = await import("@/app/api/providers/[id]/models/route.js");
    const res = await GET({}, { params: Promise.resolve({ id: "ar-1" }) });

    expect(calls[0].url).toBe("https://agentrouter.org/api/pricing");
    expect(calls[0].headers.Authorization).toBeUndefined();
    expect(res.body.models).toEqual([
      { id: "glm-5.3", name: "glm-5.3" },
      { id: "claude-opus-5", name: "claude-opus-5" },
    ]);
  });
});
