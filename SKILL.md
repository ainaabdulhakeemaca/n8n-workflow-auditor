---
name: n8n-workflow-auditor
description: Audits n8n workflow JSON exports for defects before they run in production — broken node references, orphan nodes, missing credentials, hardcoded secrets, unthrottled loops, and missing error handling. Use this skill whenever the user shares an n8n workflow JSON file or pasted n8n JSON, asks "what's wrong with this automation", asks for a workflow review, debug, audit, or sanity-check, says a workflow is failing or producing unexpected results, or asks whether an automation will survive production load. Also use it when the user is about to hand an n8n workflow to a client. Trigger even if the user does not say the word "n8n" — a JSON blob containing "nodes" and "connections" keys is an n8n export.
---

# n8n Workflow Auditor

Reviews n8n workflow exports. The point of this skill is that structural defects are
**proven with a script**, not inferred by reading JSON. LLMs reading n8n JSON reliably
invent problems and miss real ones; the linter does not.

## Workflow

### 1. Get the JSON to disk

If the user uploaded a file, use its path. If they pasted JSON, write it to a temp file.
Some exports wrap the workflow in a list — the linter handles that.

### 2. Run the linter — always, before saying anything about the workflow

```bash
python3 n8n_lint.py <workflow.json> --json
```

Do not describe the workflow, comment on it, or answer any question about it before the
linter has run. If the JSON fails to parse, report the line and column from the error and stop.

### 3. Report

Use the linter output as the factual base. Structure:

1. **Verdict line** — one of: "Will fail on run" / "Runs, but breaks under load" / "Runs; issues are cosmetic"
2. **Table** — `Severity | Node | Problem | Fix`, BLOCKERs first
3. **Corrected JSON** — for BLOCKERs only, the fixed `parameters` block for that node. Nothing else.
4. **What breaks in production** — max 5 bullets, specific to this workflow: rate limits, token expiry, empty arrays, partial failure, duplicate runs.

Keep it short. No preamble, no summarising the workflow back at the user, no praise.
If nothing is wrong, say so in one line rather than manufacturing findings.

## Rules about what you may claim

- **Every BLOCKER and WARNING from the linter must appear in the report.**
- You may add findings the linter cannot detect — bad logic, wrong API for the job, missing
  pagination, no dedupe, no idempotency key. Label these **"Unverified — judgement call."**
- **Never report a structural defect that the linter did not flag.** If you believe you see a
  broken reference, orphan node, or bad connection that the linter missed, say so explicitly
  and ask the user to confirm rather than asserting it.
- Sticky notes (`stickyNote`) are excluded from every check. Never report one as a defect.
- `missing-credentials` fires on every public template, because exports strip credential
  data. When auditing a template someone published, treat it as expected, not a finding.
- The `field-mismatch` check is a heuristic and false-positives whenever data comes from an
  API response. Never present it as certain, and never promote it to BLOCKER.

## What you cannot verify — say so instead of guessing

- Whether a node's `typeVersion` is current, or whether a given parameter name exists in the
  installed n8n version. Say "verify against the node's docs" — do not assert it's wrong.
- The shape of data returned by any external API.
- n8n pricing, self-hosting limits, or which nodes ship in the current release.

## Checks the linter performs

| Check | Severity | Provable? |
|---|---|---|
| `$node["X"]` / `$('X')` / `$items("X")` referencing a node that doesn't exist | BLOCKER | yes |
| Connection pointing at a node not in `nodes[]` | BLOCKER | yes |
| Duplicate node names (ambiguous references) | BLOCKER | yes |
| Node with no incoming connection and not a trigger (silently never runs) | BLOCKER | yes |
| No trigger node at all | BLOCKER | yes |
| Credentialed node type with no credentials attached | BLOCKER | yes |
| Hardcoded API key / bearer token in parameters | BLOCKER | yes |
| Code node JavaScript that does not parse (`node --check`) | BLOCKER | yes |
| `$json.field` with no upstream node emitting that key | WARNING | heuristic |
| Network node with no `retryOnFail` and no `onError` | WARNING | yes |
| Split In Batches feeding HTTP calls with no Wait node | WARNING | yes |
| Disabled nodes, leftover `pinData` | WARNING | yes |
| Writes to a system of record with no IF/Switch/Filter upstream | WARNING | yes |
| Non-trigger node with empty `parameters` | NIT | yes |

The linter only reasons about graph structure and expressions, because those are stable
across n8n releases. It deliberately does not validate parameter names.

## Extending it

If the user wants a new check, add it to `scripts/n8n_lint.py` and the table above. The bar
for a BLOCKER is that it must be provable from the JSON alone with no assumptions about
runtime data. Anything requiring a guess is a WARNING at most.
