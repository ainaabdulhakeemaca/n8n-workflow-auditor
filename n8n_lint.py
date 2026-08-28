#!/usr/bin/env python3
"""
n8n_lint.py — deterministic structural validator for n8n workflow JSON exports.

Usage:
    python n8n_lint.py workflow.json           # human-readable
    python n8n_lint.py workflow.json --json    # machine-readable (for an LLM to consume)

Only checks things that can be proven from the JSON alone. It deliberately does NOT
validate node parameter names or typeVersions, because those change between n8n
releases and guessing there is how you get confident, wrong output.
"""

import json
import re
import sys
from collections import defaultdict

# Node types that essentially always require credentials.
CRED_REQUIRED = (
    "googleSheets", "gmail", "googleDrive", "googleCalendar", "slack", "airtable",
    "notion", "hubspot", "salesforce", "stripe", "shopify", "openAi", "anthropic",
    "postgres", "mysql", "mongoDb", "redis", "supabase", "telegram", "twilio",
    "microsoftOutlook", "microsoftExcel", "clickUp", "asana", "trello", "pipedrive",
    "mailchimp", "sendGrid", "zendesk", "jira", "s3", "dropbox", "linkedIn",
)

# Node types that write to a system of record or send something externally.
SIDE_EFFECT = (
    "googleSheets", "gmail", "slack", "airtable", "notion", "postgres", "mysql",
    "mongoDb", "sendGrid", "telegram", "twilio", "emailSend", "hubspot", "salesforce",
    "microsoftOutlook", "linkedIn", "discord", "webhook",
)

TRIGGER_HINTS = ("trigger", "webhook", "cron", "schedule", "interval", "formTrigger",
                 "executeWorkflowTrigger", "manualTrigger", "errorTrigger", "chatTrigger",
                 # nodes that ARE triggers but don't say so in the type name
                 "emailReadImap", "rssFeedRead", "localFileTrigger", "sseTrigger")

# Nodes that exist only as canvas documentation. They are never connected and never
# execute. Excluded from every check — flagging them is pure noise.
COSMETIC_TYPES = ("stickyNote",)

# Operation values that actually write or send. A Sheets node set to "read" is not a
# write; neither is a trigger that happens to be a Gmail node type.
READ_ONLY_OPERATIONS = (
    "read", "get", "getall", "getmany", "search", "lookup", "download", "list",
)

HTTP_LIKE = ("httpRequest", "graphql", "webhook")

SECRET_PATTERNS = [
    (r"sk-[A-Za-z0-9_\-]{16,}", "OpenAI-style key"),
    (r"sk-ant-[A-Za-z0-9_\-]{16,}", "Anthropic key"),
    (r"(?i)bearer\s+[A-Za-z0-9_\-\.]{20,}", "Bearer token"),
    (r"(?i)(api[_-]?key|apikey|access[_-]?token|auth[_-]?token)\s*[=:]\s*[\"']?[A-Za-z0-9_\-]{12,}", "inline API key"),
    (r"xox[baprs]-[A-Za-z0-9\-]{10,}", "Slack token"),
    (r"AIza[0-9A-Za-z_\-]{30,}", "Google API key"),
    (r"ghp_[A-Za-z0-9]{20,}", "GitHub token"),
]

# $node["Name"] / $node.Name / $('Name') / $items("Name") / $("Name").item
NODE_REF_PATTERNS = [
    re.compile(r"\$node\[\s*[\"']([^\"']+)[\"']\s*\]"),
    re.compile(r"\$items\(\s*[\"']([^\"']+)[\"']"),
    re.compile(r"\$\(\s*[\"']([^\"']+)[\"']\s*\)"),
]
JSON_FIELD_PATTERNS = [
    re.compile(r"\$json\[\s*[\"']([^\"']+)[\"']\s*\]"),
    re.compile(r"\$json\.([A-Za-z_][A-Za-z0-9_]*)"),
]


def walk_strings(obj, path=""):
    """Yield (json_path, string) for every string in a nested structure."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield from walk_strings(v, f"{path}.{k}" if path else str(k))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from walk_strings(v, f"{path}[{i}]")
    elif isinstance(obj, str):
        yield path, obj


class Finding:
    def __init__(self, severity, node, check, problem, fix, path=""):
        self.severity, self.node, self.check = severity, node, check
        self.problem, self.fix, self.path = problem, fix, path

    def as_dict(self):
        return {"severity": self.severity, "node": self.node, "check": self.check,
                "problem": self.problem, "fix": self.fix, "json_path": self.path}


def lint(wf):
    f = []
    nodes = wf.get("nodes")
    if not isinstance(nodes, list):
        return [Finding("BLOCKER", "-", "schema", "No 'nodes' array — this is not an n8n workflow export.",
                        "Re-export via Workflow menu > Download.")]
    connections = wf.get("connections", {}) or {}

    # Sticky notes are documentation, not workflow. Drop them before any check runs.
    nodes = [n for n in nodes
             if not any(c.lower() in (n.get("type") or "").lower() for c in COSMETIC_TYPES)]

    names = [n.get("name", "<unnamed>") for n in nodes]
    name_set = set(names)
    by_name = {n.get("name"): n for n in nodes}

    # --- 3. duplicate names -------------------------------------------------
    counts = defaultdict(int)
    for n in names:
        counts[n] += 1
    for n, c in counts.items():
        if c > 1:
            f.append(Finding("BLOCKER", n, "duplicate-name",
                             f"{c} nodes share the name '{n}'. Every $node['{n}'] reference is ambiguous.",
                             "Rename so each node name is unique, then update expressions."))

    # --- 2. connections pointing at nothing ---------------------------------
    for src, outs in connections.items():
        if src not in name_set:
            f.append(Finding("BLOCKER", src, "dangling-connection",
                             f"connections contains source '{src}' which is not in nodes[].",
                             "Delete the stale connection entry or restore the node.",
                             f"connections.{src}"))
        for out_type, branches in (outs or {}).items():
            for bi, branch in enumerate(branches or []):
                for ci, conn in enumerate(branch or []):
                    tgt = conn.get("node")
                    if tgt not in name_set:
                        f.append(Finding("BLOCKER", src, "dangling-connection",
                                         f"'{src}' connects to '{tgt}', which does not exist.",
                                         "Reconnect to a real node or remove the link.",
                                         f"connections.{src}.{out_type}[{bi}][{ci}]"))

    # --- 4/5. triggers and orphans -----------------------------------------
    has_incoming = set()
    for src, outs in connections.items():
        for branches in (outs or {}).values():
            for branch in branches or []:
                for conn in branch or []:
                    if conn.get("node"):
                        has_incoming.add(conn["node"])

    def is_trigger(n):
        t = (n.get("type") or "")
        return any(h.lower() in t.lower() for h in TRIGGER_HINTS)

    if not any(is_trigger(n) for n in nodes):
        f.append(Finding("BLOCKER", "-", "no-trigger",
                         "Workflow contains no trigger node. It can only ever be run manually.",
                         "Add a Schedule/Webhook/App trigger."))

    for n in nodes:
        nm = n.get("name")
        if nm not in has_incoming and not is_trigger(n) and not n.get("disabled"):
            f.append(Finding("BLOCKER", nm, "orphan-node",
                             "No incoming connection and not a trigger — this node never executes. "
                             "n8n does not warn you about this.",
                             "Connect it upstream or delete it."))

    # --- 1. broken node references in expressions ---------------------------
    for n in nodes:
        nm = n.get("name")
        for path, s in walk_strings(n.get("parameters", {}), "parameters"):
            for pat in NODE_REF_PATTERNS:
                for ref in pat.findall(s):
                    if ref not in name_set:
                        f.append(Finding("BLOCKER", nm, "broken-node-ref",
                                         f"Expression references node '{ref}', which does not exist "
                                         f"(node names are case- and space-sensitive).",
                                         f"Fix the name. Closest existing: "
                                         f"{_closest(ref, names) or 'none'}.",
                                         path))

    # --- 6. $json fields with no upstream producer (heuristic) --------------
    produced = _produced_fields(nodes)
    external = _nodes_with_external_data(nodes)
    for n in nodes:
        nm = n.get("name")
        upstream_external = _has_external_upstream(nm, connections, external, by_name)
        for path, s in walk_strings(n.get("parameters", {}), "parameters"):
            for pat in JSON_FIELD_PATTERNS:
                for fld in pat.findall(s):
                    if fld in produced or upstream_external:
                        continue
                    f.append(Finding("WARNING", nm, "field-mismatch",
                                     f"References $json field '{fld}' but no upstream Set/Code node "
                                     f"emits that key. Known upstream keys: "
                                     f"{sorted(produced)[:12] or 'none found'}.",
                                     "Confirm the field name against real run data. Heuristic check — "
                                     "false-positives when data comes from an API response.",
                                     path))

    # --- 7. missing credentials ---------------------------------------------
    for n in nodes:
        t = n.get("type", "")
        if any(c.lower() in t.lower() for c in CRED_REQUIRED) and not n.get("credentials"):
            f.append(Finding("BLOCKER", n.get("name"), "missing-credentials",
                             f"Node type {t} needs credentials but none are attached.",
                             "Attach a credential in the node. Note exports strip credential "
                             "*data* but keep the reference — an empty block means it was never set."))

    # --- 8. hardcoded secrets ------------------------------------------------
    for n in nodes:
        for path, s in walk_strings(n.get("parameters", {}), "parameters"):
            for pat, label in SECRET_PATTERNS:
                if re.search(pat, s):
                    f.append(Finding("BLOCKER", n.get("name"), "hardcoded-secret",
                                     f"Possible {label} hardcoded in workflow JSON.",
                                     "Move to an n8n credential or environment variable. Assume any "
                                     "key that has been in an exported file is compromised — rotate it.",
                                     path))
                    break

    # --- 9. no error handling on network nodes -------------------------------
    for n in nodes:
        t = n.get("type", "")
        networky = any(h.lower() in t.lower() for h in HTTP_LIKE) or \
                   any(c.lower() in t.lower() for c in CRED_REQUIRED)
        if is_trigger(n):
            networky = False      # retryOnFail is not the fix for a failing trigger
        if networky and not n.get("onError") and not n.get("continueOnFail") and not n.get("retryOnFail"):
            f.append(Finding("WARNING", n.get("name"), "no-error-handling",
                             "Network call with no retryOnFail and no onError. One 429 or 503 "
                             "kills the entire execution mid-batch.",
                             "Set retryOnFail: true (2-3 tries) and onError: 'continueErrorOutput', "
                             "then handle the error branch."))

    # --- 10. loop without wait ------------------------------------------------
    loop_nodes = [n.get("name") for n in nodes if "splitInBatches" in (n.get("type") or "")]
    has_wait = any("wait" in (n.get("type") or "").lower() for n in nodes)
    http_nodes = [n.get("name") for n in nodes
                  if any(h.lower() in (n.get("type") or "").lower() for h in HTTP_LIKE)]
    if loop_nodes and http_nodes and not has_wait:
        f.append(Finding("WARNING", loop_nodes[0], "loop-no-throttle",
                         "Split In Batches feeding HTTP calls with no Wait node. This will hit "
                         "rate limits as soon as the input list grows.",
                         "Add a Wait node inside the loop, or set batch size to 1 with a delay."))

    # --- 11. disabled nodes / pinned data ------------------------------------
    for n in nodes:
        if n.get("disabled"):
            f.append(Finding("WARNING", n.get("name"), "disabled-node",
                             "Node is disabled. Data passes straight through it.",
                             "Enable it or delete it before shipping."))
    if wf.get("pinData"):
        f.append(Finding("WARNING", "-", "pinned-data",
                         f"Pinned data present on: {list(wf['pinData'].keys())}. Manual executions "
                         f"use the pinned values, so the workflow tests green and fails in production.",
                         "Unpin before handover."))

    # --- 12. writes with no gate ---------------------------------------------
    gate_types = ("if", "switch", "filter", "noOp")
    for n in nodes:
        t = (n.get("type") or "")
        op = str((n.get("parameters") or {}).get("operation") or "").lower().replace("_", "")
        if is_trigger(n) or op in READ_ONLY_OPERATIONS:
            continue                      # a read or a trigger is not a write
        if any(s.lower() in t.lower() for s in SIDE_EFFECT):
            if not _has_upstream_of_type(n.get("name"), connections, by_name, gate_types):
                f.append(Finding("WARNING", n.get("name"), "ungated-write",
                                 "Writes/sends externally with no IF, Switch or Filter upstream. "
                                 "Every item that reaches it goes out — including empty or malformed ones.",
                                 "Add a filter, or an approval column checked by an IF node."))

    # --- 14. JavaScript syntax in Code nodes ---------------------------------
    # Provable: either the code parses or it does not. Requires node on PATH;
    # if node is missing the check is skipped entirely rather than guessed at.
    for n in nodes:
        t = (n.get("type") or "").lower()
        if "code" not in t and "function" not in t:
            continue
        p_ = n.get("parameters", {}) or {}
        code = p_.get("jsCode") or p_.get("functionCode")
        if not code:
            continue
        err = _js_syntax_error(code)
        if err:
            f.append(Finding("BLOCKER", n.get("name"), "code-syntax-error",
                             f"JavaScript does not parse: {err}",
                             "Fix the syntax. This node throws on every execution — "
                             "the workflow cannot run at all.",
                             "parameters.jsCode"))

    # --- 13. empty parameters -------------------------------------------------
    for n in nodes:
        if not n.get("parameters") and not is_trigger(n) and "noOp" not in (n.get("type") or ""):
            f.append(Finding("NIT", n.get("name"), "empty-parameters",
                             f"Node {n.get('type')} has empty parameters — likely never configured.",
                             "Open the node and configure, or remove."))

    return f


# ---------- helpers ----------------------------------------------------------

def _js_syntax_error(code):
    """Return a one-line syntax error for JS source, or None if it parses / can't check.

    Uses `node --check`. If node isn't installed we return None: a skipped check is
    correct, a guessed one is not.
    """
    import shutil, subprocess, tempfile, os
    exe = shutil.which("node") or shutil.which("nodejs")
    if not exe:
        return None
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(code)
        r = subprocess.run([exe, "--check", path],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return None
        for line in r.stderr.splitlines():
            line = line.strip()
            if line.startswith(("SyntaxError", "ReferenceError", "TypeError")):
                return line
        return "SyntaxError (see `node --check` for detail)"
    except Exception:
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass



def _produced_fields(nodes):
    """Field names assigned by Set / Code nodes. Heuristic, best-effort."""
    out = set()
    for n in nodes:
        t = (n.get("type") or "").lower()
        p = n.get("parameters", {}) or {}
        if "set" in t:
            assigns = (p.get("assignments") or {}).get("assignments") or []
            for a in assigns:
                if a.get("name"):
                    out.add(str(a["name"]).split(".")[-1])
            for group in (p.get("values") or {}).values():   # older Set node
                if isinstance(group, list):
                    for a in group:
                        if isinstance(a, dict) and a.get("name"):
                            out.add(str(a["name"]).split(".")[-1])
        if "code" in t or "function" in t:
            code = p.get("jsCode") or p.get("functionCode") or p.get("pythonCode") or ""
            out |= set(re.findall(r"[\"']?([A-Za-z_][A-Za-z0-9_]*)[\"']?\s*:", code))
    return out


def _nodes_with_external_data(nodes):
    return {n.get("name") for n in nodes
            if any(h.lower() in (n.get("type") or "").lower() for h in HTTP_LIKE)
            or any(c.lower() in (n.get("type") or "").lower() for c in CRED_REQUIRED)}


def _incoming(target, connections):
    src = []
    for s, outs in connections.items():
        for branches in (outs or {}).values():
            for branch in branches or []:
                for conn in branch or []:
                    if conn.get("node") == target:
                        src.append(s)
    return src


def _has_external_upstream(name, connections, external, by_name, depth=0, seen=None):
    seen = seen or set()
    if depth > 12 or name in seen:
        return False
    seen.add(name)
    for src in _incoming(name, connections):
        if src in external:
            return True
        if _has_external_upstream(src, connections, external, by_name, depth + 1, seen):
            return True
    return False


def _has_upstream_of_type(name, connections, by_name, type_hints, depth=0, seen=None):
    seen = seen or set()
    if depth > 12 or name in seen:
        return False
    seen.add(name)
    for src in _incoming(name, connections):
        t = (by_name.get(src, {}).get("type") or "").lower()
        if any(h.lower() in t for h in type_hints):
            return True
        if _has_upstream_of_type(src, connections, by_name, type_hints, depth + 1, seen):
            return True
    return False


def _closest(target, candidates):
    best, score = None, 0.0
    tl = target.lower()
    for c in candidates:
        cl = c.lower()
        common = len(set(tl) & set(cl))
        s = common / max(len(set(tl) | set(cl)), 1)
        if cl.replace(" ", "") == tl.replace(" ", ""):
            return c
        if s > score:
            best, score = c, s
    return best if score > 0.6 else None


def main():
    if len(sys.argv) < 2:
        print("usage: python n8n_lint.py workflow.json [--json]")
        sys.exit(2)
    with open(sys.argv[1], "r", encoding="utf-8") as fh:
        try:
            wf = json.load(fh)
        except json.JSONDecodeError as e:
            print(f"INVALID JSON at line {e.lineno} col {e.colno}: {e.msg}")
            sys.exit(1)

    if isinstance(wf, list):          # some exports wrap in a list
        wf = wf[0]

    findings = lint(wf)
    order = {"BLOCKER": 0, "WARNING": 1, "NIT": 2}
    findings.sort(key=lambda x: order.get(x.severity, 3))

    if "--json" in sys.argv:
        print(json.dumps({
            "node_count": len(wf.get("nodes", [])),
            "blockers": sum(1 for x in findings if x.severity == "BLOCKER"),
            "warnings": sum(1 for x in findings if x.severity == "WARNING"),
            "findings": [x.as_dict() for x in findings],
        }, indent=2))
        return

    b = sum(1 for x in findings if x.severity == "BLOCKER")
    print(f"\n{len(wf.get('nodes', []))} nodes | {b} blockers | "
          f"{sum(1 for x in findings if x.severity == 'WARNING')} warnings\n")
    for x in findings:
        print(f"[{x.severity}] {x.node} — {x.check}")
        print(f"  problem: {x.problem}")
        print(f"  fix:     {x.fix}")
        if x.path:
            print(f"  at:      {x.path}")
        print()
    if b:
        sys.exit(1)


if __name__ == "__main__":
    main()
