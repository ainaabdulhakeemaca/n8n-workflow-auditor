# n8n Workflow Auditor

A structural linter for n8n workflow JSON exports. It finds the defects that make a workflow
fail on run or die quietly under load — broken node references, orphan nodes, missing
credentials, hardcoded secrets, unthrottled loops, ungated writes.

## Why it exists

Ask an LLM to review an n8n export and it will invent problems and miss real ones. It will tell
you a node has no incoming connection when it does, and stay silent about the `$node["Enrich
Comapny"]` typo that will throw on the first run.

That's not a prompting problem. Graph structure is *provable* — a node reference either resolves
against `nodes[]` or it doesn't — and asking a language model to do provable work is the wrong
tool for the job.

So this splits the work:

- **The script proves defects.** Deterministic checks over the graph and expressions. No inference.
- **The model explains only what's proven.** It can add judgement calls, but they're labelled as
  such, and it is forbidden from reporting a structural defect the linter didn't flag.

That constraint is the whole design. Everything else is checks.

## What it catches

| Check | Severity | Provable |
|---|---|---|
| `$node["X"]` / `$('X')` / `$items("X")` referencing a node that doesn't exist | BLOCKER | yes |
| Connection pointing at a node not in `nodes[]` | BLOCKER | yes |
| Duplicate node names — every reference to them is ambiguous | BLOCKER | yes |
| Node with no incoming connection and not a trigger (never executes; n8n does not warn you) | BLOCKER | yes |
| No trigger node at all | BLOCKER | yes |
| Credentialed node type with no credentials attached | BLOCKER | yes |
| Hardcoded API key or bearer token in parameters | BLOCKER | yes |
| Network node with no `retryOnFail` and no `onError` | WARNING | yes |
| Split In Batches feeding HTTP calls with no Wait node | WARNING | yes |
| Writes to a system of record with no IF/Switch/Filter upstream | WARNING | yes |
| Disabled nodes, leftover `pinData` | WARNING | yes |
| `$json.field` with no upstream node emitting that key | WARNING | heuristic |
| Non-trigger node with empty `parameters` | NIT | yes |

## Run it

No dependencies. Python 3.8+.

```bash
python3 n8n_lint.py workflow.json           # human-readable
python3 n8n_lint.py workflow.json --json    # machine-readable, for an LLM to consume
```

Exit code is `1` when there is at least one BLOCKER, so it drops into CI unchanged.

Try it on the deliberately broken example — 8 blockers, 10 warnings:

```bash
python3 n8n_lint.py examples/broken-workflow.json
```

Full output is in [`examples/sample-output.txt`](examples/sample-output.txt).

```
[BLOCKER] Score Lead — broken-node-ref
  problem: Expression references node 'Enrich Comapny', which does not exist
           (node names are case- and space-sensitive).
  fix:     Fix the name. Closest existing: Enrich Company.
  at:      parameters.messages.values[0].content

[BLOCKER] Notify Slack — orphan-node
  problem: No incoming connection and not a trigger — this node never executes.
           n8n does not warn you about this.
  fix:     Connect it upstream or delete it.

[WARNING] Loop Leads — loop-no-throttle
  problem: Split In Batches feeding HTTP calls with no Wait node. This will hit
           rate limits as soon as the input list grows.
  fix:     Add a Wait node inside the loop, or set batch size to 1 with a delay.
```

## Limitations

Read this part. It's the useful half.

- **It does not validate parameter names or `typeVersion`.** Those change between n8n releases,
  and a linter that's confidently wrong about them is worse than no linter. Check node docs
  yourself.
- **`field-mismatch` is a heuristic and false-positives constantly** whenever data comes from an
  API response, because the script can't know the response shape. It is never a BLOCKER and
  should never be reported as certain.
- **It cannot see your logic.** Wrong API for the job, missing pagination, no dedupe key, no
  idempotency — all invisible here. A workflow can pass clean and still be a bad workflow.
- **`missing-credentials` has a known false positive.** n8n exports strip credential *data* but
  keep the reference. An entirely absent `credentials` block usually means it was never set, but
  verify before telling someone their workflow is broken.
- **The credentialed-node list is hardcoded** to ~35 common types. Uncommon nodes won't be checked.
- **It only reads one workflow.** Sub-workflows called via Execute Workflow are not followed.
- **Untested against n8n's older export schemas.** Built against current exports; a 2022 file may
  parse but check nothing useful.

## The LLM layer

The repo ships the linter. The report format and the rules constraining what a model may claim
on top of it live in [`SKILL.md`](SKILL.md) — usable as a Claude skill, or as a system prompt for
any model you point at the `--json` output.

The rule that matters: *never report a structural defect the linter did not flag.* If the model
thinks it sees one, it has to say so and ask, not assert.

## Extending

Add checks to `n8n_lint.py`. The bar for BLOCKER is that it must be provable from the JSON alone
with no assumptions about runtime data. Anything requiring a guess is a WARNING at most.

## License

MIT.
