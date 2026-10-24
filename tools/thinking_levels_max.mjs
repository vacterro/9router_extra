// Compute the max-safe thinking level per provider from a provider->models JSON:
//   {"<providerId>": ["<modelId>", ...], ...}
// Emits {"patch": {...}, "report": [...]} on stdout.
//
// Rule: the highest level every reasoning model of a provider declares
// (intersection of the source's ladders). Empty intersection (mixed onOff +
// numeric ladders) collapses to "high" — the one level every provider-native
// format maps without rejection. "thinking" is kept for pure onOff providers
// (zai/minimax): it is their "enabled" ceiling.
import { readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import path from "node:path";

const SRC = process.env.NINEROUTER_SOURCE || "C:/Users/vac34/AppData/Roaming/9router/source";
const { getThinkingLevels } = await import(
  pathToFileURL(path.join(SRC, "open-sse/providers/thinkingLevels.js")).href
);

const data = JSON.parse(readFileSync(process.argv[2], "utf-8"));

const RANK = { none: 0, minimal: 1, low: 2, medium: 3, high: 4, thinking: 4, xhigh: 5, max: 6, ultra: 7 };
const NUMERIC = new Set(["minimal", "low", "medium", "high", "xhigh", "max", "ultra"]);

const patch = {};
const report = [];
for (const [provider, modelIds] of Object.entries(data)) {
  const ladders = [];
  for (const id of modelIds) {
    let lv = null;
    try {
      lv = getThinkingLevels(provider, id);
    } catch {
      lv = null;
    }
    if (lv) {
      const set = new Set(lv.filter((l) => l !== "none"));
      if (set.size) ladders.push(set);
    }
  }
  if (!ladders.length) continue;

  const union = new Set();
  for (const s of ladders) for (const l of s) union.add(l);

  let inter = ladders[0];
  for (const s of ladders.slice(1)) inter = new Set([...inter].filter((l) => s.has(l)));

  let mode;
  let rule;
  if (inter.size) {
    mode = [...inter].sort((a, b) => (RANK[a] ?? -1) - (RANK[b] ?? -1)).pop();
    rule = "intersection";
  } else if ([...union].every((l) => !NUMERIC.has(l))) {
    mode = "thinking";
    rule = "all-onOff";
  } else {
    mode = "high";
    rule = "empty-intersection->high";
  }

  patch[provider] = { mode };
  report.push({
    provider,
    mode,
    rule,
    reasoningModels: ladders.length,
    models: modelIds.length,
    union: [...union].sort((a, b) => (RANK[a] ?? -1) - (RANK[b] ?? -1)),
  });
}

report.sort((a, b) => a.provider.localeCompare(b.provider));
console.log(JSON.stringify({ patch, report }, null, 1));
