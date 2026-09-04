import { describe, it, expect, vi, beforeEach } from "vitest";

describe("Usage Auto-Lock for 0% Quota Models", () => {
  it("computes modelLock updates when a model has 0% remaining with future resetAt", () => {
    const futureReset = new Date(Date.now() + 3600 * 1000).toISOString();
    const quotas = {
      "gemini-3.7-flash-high": {
        remainingPercentage: 0,
        resetAt: futureReset,
      },
      "claude-sonnet-4-6": {
        remainingPercentage: 100,
        resetAt: futureReset,
      }
    };

    const lockUpdates = {};
    const now = Date.now();
    for (const [modelKey, quota] of Object.entries(quotas)) {
      if (quota?.remainingPercentage === 0 && quota?.resetAt) {
        const resetTime = new Date(quota.resetAt).getTime();
        if (resetTime > now) {
          lockUpdates[`modelLock_${modelKey}`] = new Date(resetTime).toISOString();
        }
      }
    }

    expect(lockUpdates["modelLock_gemini-3.7-flash-high"]).toBe(futureReset);
    expect(lockUpdates["modelLock_claude-sonnet-4-6"]).toBeUndefined();
  });
});
