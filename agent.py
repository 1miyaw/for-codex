#!/usr/bin/env python3
"""Five-role omics task team: literature, data, design, blind solve, review."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request

import prompts
import team
import evaluate

CHECKS = ("atomic_target", "data_dependency", "judgment", "no_leakage", "evidence_scope")
TRACKS = ("method_reproduction", "result_reproduction", "exploration")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def text(value):
    return isinstance(value, str) and bool(value.strip())


def strings(value, nonempty=False):
    return isinstance(value, list) and (not nonempty or bool(value)) and all(text(v) for v in value)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def indexed(rows, label):
    require(isinstance(rows, list), label + " must be a list")
    require(all(isinstance(r, dict) and text(r.get("id")) for r in rows), label + " needs IDs")
    result = {r["id"]: r for r in rows}
    require(len(result) == len(rows), label + " IDs must be unique")
    return result


def load_input(path):
    spec = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(spec, dict), "input must be an object")
    require(text(spec.get("title")), "title is required")
    require(spec.get("track") in TRACKS, "unsupported track")
    require(type(spec.get("max_tasks")) is int and 1 <= spec["max_tasks"] <= 10,
            "max_tasks must be an integer between 1 and 10")
    sources = indexed(spec.get("sources"), "sources")
    data = indexed(spec.get("data"), "data")
    require(bool(sources) and bool(data), "provide sources and declared data capabilities")
    for s in sources.values():
        require(text(s.get("citation")) and text(s.get("text")), "source needs citation and text")
    manifest = {}
    for key, item in data.items():
        require(text(item.get("description")), "data needs a description")
        raw = item.get("path")
        require(raw is None or text(raw), "data path must be null or a local file path")
        p = (path.parent / raw).resolve() if raw else None
        exists = bool(p and p.is_file())
        manifest[key] = {"available": exists, "path": str(p) if p else None,
                         "sha256": file_digest(p) if exists else None}
    return spec, manifest


def validate_evidence(result, spec):
    require(isinstance(result, dict), "extraction must be an object")
    evidence = indexed(result.get("evidence"), "evidence")
    require(strings(result.get("gaps")), "extraction gaps must be a string list")
    require(bool(evidence) or bool(result["gaps"]), "empty extraction must explain its gaps")
    sources = indexed(spec["sources"], "sources")
    for e in evidence.values():
        require(e.get("source_id") in sources, "unknown evidence source")
        for field in ("quote", "claim", "derivation", "limitations"):
            require(text(e.get(field)), "evidence missing " + field)
        require(e["quote"] in sources[e["source_id"]]["text"],
                "evidence quote is not an exact source substring: " + e["id"])
    return evidence


def validate_tasks(draft, spec, evidence, previous_ids=None):
    require(isinstance(draft, dict), "draft must be an object")
    tasks = indexed(draft.get("tasks"), "tasks")
    require(len(tasks) <= spec["max_tasks"], "too many tasks")
    require(strings(draft.get("gaps")), "draft gaps must be a string list")
    require(bool(tasks) or bool(draft["gaps"]), "zero tasks must explain their gaps")
    if previous_ids is not None:
        require(set(tasks) == set(previous_ids), "revision must preserve task IDs")
    for t in tasks.values():
        for field in ("objective", "question", "reference_answer", "acceptance"):
            require(text(t.get(field)), t["id"] + " missing " + field)
        for field, allowed in (("evidence_ids", evidence), ("data_ids", indexed(spec["data"], "data"))):
            require(strings(t.get(field), True), t["id"] + " missing " + field)
            require(set(t[field]) <= set(allowed), t["id"] + " unknown " + field)
            require(len(t[field]) == len(set(t[field])), "duplicate references")
        require(strings(t.get("unresolved")), "unresolved must be a string list")
        milestones = indexed(t.get("milestones"), "milestones")
        require(len(milestones) >= 2, "need dependent milestones, not one-shot QA")
        visited, decisions, edges = set(), 0, 0
        graph = {key: set() for key in milestones}
        for m in milestones.values():
            evaluate.check_artifact(m.get("artifact"))
            for field in ("goal", "artifact", "acceptance"):
                require(text(m.get(field)), "milestone missing " + field)
            require(isinstance(m.get("decision"), str), "decision must be a string")
            decisions += bool(m["decision"].strip())
            deps = m.get("depends_on")
            require(strings(deps), "depends_on must be a string list")
            require(len(deps) == len(set(deps)), "duplicate milestone dependencies")
            require(set(deps) <= visited, "dependencies must refer to earlier milestones (no cycles)")
            for dep in deps:
                graph[m["id"]].add(dep)
                graph[dep].add(m["id"])
            edges += len(deps)
            visited.add(m["id"])
        require(decisions and edges, "need both a decision and a dependency")
        reached, queue = set(), [next(iter(milestones))]
        while queue:
            node = queue.pop()
            if node not in reached:
                reached.add(node)
                queue.extend(graph[node] - reached)
        require(reached == set(milestones), "milestones must form one connected task")
    return tasks


def validate_reviews(result, tasks):
    require(isinstance(result, dict) and isinstance(result.get("reviews"), list), "invalid review")
    reviews = {}
    for r in result["reviews"]:
        require(isinstance(r, dict) and r.get("task_id") in tasks, "unknown reviewed task")
        require(r["task_id"] not in reviews, "duplicate review")
        require(r.get("verdict") in ("pass", "revise", "reject"), "invalid verdict")
        require(text(r.get("feedback")), "review must explain its decision")
        checks = r.get("checks")
        require(isinstance(checks, dict) and set(checks) == set(CHECKS), "all five checks required")
        for check in checks.values():
            require(isinstance(check, dict) and type(check.get("pass")) is bool
                    and text(check.get("reason")), "each check needs boolean pass and reason")
        require(r["verdict"] != "pass" or all(c["pass"] for c in checks.values()),
                "pass verdict conflicts with failed check")
        reviews[r["task_id"]] = r
    require(set(reviews) == set(tasks), "review must cover every task")
    return reviews


class Client:
    def __init__(self, model, base_url, timeout):
        self.model, self.timeout = model, timeout
        self.key = os.environ.get("OPENAI_API_KEY")
        require(text(model), "set --model or OPENAI_MODEL")
        require(text(self.key), "set OPENAI_API_KEY; GitHub login is not an LLM API key")
        parsed = urllib.parse.urlsplit(base_url)
        require(parsed.scheme == "https" or
                (parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1", "::1")),
                "API endpoint must use HTTPS (HTTP allowed only on localhost)")
        require(bool(parsed.netloc) and not (parsed.username or parsed.password or parsed.query or parsed.fragment),
                "invalid API base URL")
        self.url = base_url.rstrip("/") + "/chat/completions"

    def __call__(self, stage, system, payload):
        body = {"model": self.model, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
            "response_format": {"type": "json_object"}, "max_completion_tokens": 8000}
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Authorization": "Bearer " + self.key,
                                                  "Content-Type": "application/json"})
        # Do not follow redirects with an Authorization header.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=self.timeout) as response:
                data = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError("LLM HTTP %s; checkpoint retained, no automatic retry" % exc.code) from None
        choice = data["choices"][0]
        require(choice.get("finish_reason") == "stop", "LLM response incomplete or refused")
        require(text(choice.get("message", {}).get("content")), "empty LLM output")
        return choice["message"]["content"]


def demo_response(stage, system, payload):
    """A deterministic synthetic fixture, not a simulated successful LLM experiment."""
    spec = payload["input"]
    if stage == "extract":
        result = {"evidence": [{"id": "E1", "source_id": "S1",
                  "quote": "The authors reported reduced interferon-response activity after treatment, while the magnitude varied across donors.",
                  "claim": "合成论文声称治疗后干扰素反应下降，但供体间效应不同。",
                  "derivation": "合成论文描述供体内比较与基因集分析。",
                  "limitations": "无真实数据、效应量或显著性结果，不能作为实验结论。"}],
                  "gaps": ["缺少真实组学数据与实际运行证据。"]}
    elif stage.startswith("review"):
        result = {"reviews": [{"task_id": t["id"], "verdict": "pass",
                  "checks": {k: {"pass": True, "reason": "离线固定夹具，用于检查流程接口；不是模型审查。"}
                             for k in CHECKS},
                  "feedback": "仅测试结构；需真实数据、真实模型审查和执行验证。"}
                 for t in payload["draft"]["tasks"]]}
    else:
        result = {"tasks": [{"id": "T1", "objective": "评估配对治疗样本中的干扰素反应变化是否稳健。",
                  "question": "利用配对 bulk RNA 数据，评估治疗前后干扰素反应的变化，并以供体层面的证据支持结论。",
                  "evidence_ids": ["E1"], "data_ids": ["D1"],
                  "milestones": [
                      {"id": "M1", "goal": "确定可用的配对比较集合。", "depends_on": [],
                       "decision": "判断缺失配对和批次与治疗是否混杂，并说明纳入依据。",
                       "artifact": "paired_samples.csv", "acceptance": "配对、批次和排除原因可追溯；不可识别时报告限制。"},
                      {"id": "M2", "goal": "估计治疗相关的通路变化。", "depends_on": ["M1"],
                       "decision": "依据样本单位和数据分布选择配对估计方法。",
                       "artifact": "pathway_effects.csv", "acceptance": "报告效应与不确定性；无差异也是允许的结果。"},
                      {"id": "M3", "goal": "判断结论对供体异质性是否稳健。", "depends_on": ["M2"],
                       "decision": "比较敏感性分析与主分析，裁决是否足以支持总体结论。",
                       "artifact": "robustness.json", "acceptance": "保留不一致和阴性证据，不能只保留支持预期的分析。"}],
                  "reference_answer": "仅为合成论文预期：治疗后干扰素反应下降，效应存在供体差异；尚未验证。",
                  "acceptance": "依据实际数据和稳健性证据评分；合理阴性或替代结论允许成立。",
                  "unresolved": ["需真实表达矩阵、配对及批次元数据，需明确基因集及版本。"]}],
                  "gaps": ["演示任务不能直接加入 benchmark。"]}
    return json.dumps(result, ensure_ascii=False)


def demo_team(stage, system, request):
    """Scripted agent/tool loop; no LLM and no scientific execution."""
    payload, history = request["context"], request["history"]
    if stage == "extract" and not history:
        return json.dumps({"action": "tool", "name": "read_source",
                           "arguments": {"source_id": payload["sources"][0]["id"]}})
    if stage == "audit":
        data = payload["data"]
        if len(history) < len(data):
            return json.dumps({"action": "tool", "name": "inspect_data",
                               "arguments": {"data_id": data[len(history)]["id"]}})
        result = {"datasets": [{"data_id": d["id"],
                  "status": "uncertain" if payload["files"][d["id"]]["available"] else "missing",
                  "observed": ["离线夹具，真实工具预览见轨迹。"],
                  "limitations": ["不是模型或科研验证。"]} for d in data]}
    else:
        result = json.loads(demo_response(stage, system, dict(payload, input=payload.get("input", {}))))
    return json.dumps({"action": "final", "result": result}, ensure_ascii=False)


def run(spec, manifest, out, call, config):
    fingerprint = digest({"input": spec, "data": manifest, "config": config,
                          "code": [file_digest(Path(module.__file__))
                                   for module in (sys.modules[__name__], prompts, team, evaluate)]})
    state_path = out / "state.json"
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {
        "fingerprint": fingerprint, "config": config, "calls": {}, "bundles": {}}
    require(state.get("fingerprint") == fingerprint, "input/model/code/data changed; choose a new --out directory")
    save = lambda: write_json(state_path, state)

    def step(name, role, system, payload, tools=None):
        record = state["calls"].setdefault(name, {})
        client = call.get(role) if isinstance(call, dict) else call
        require(callable(client), "missing client for " + role)
        actor = team.Agent(role, client, tools, config.get("max_turns", 8))
        return actor.run(name, system, payload, record, save)

    def capture(name, collect):
        if name not in state["bundles"]:
            state["bundles"][name] = collect()
            save()
        return state["bundles"][name]

    step.capture = capture
    step.bundle = lambda name: state["bundles"].get(name)
    step.started = lambda name: bool(state["calls"].get(name))
    step.artifact_dir = lambda name: out / "artifacts" / digest(name)[:16]
    base = {"input": spec, "file_manifest": manifest}
    try:
        # Literature reads sources; data specialist never sees source conclusions.
        literature_input = {"title": spec["title"], "sources": [
            {"id": s["id"], "citation": s["citation"], "characters": len(s["text"])}
            for s in spec["sources"]]}
        extraction = step("extract", "literature", prompts.EXTRACT, literature_input,
                          team.source_tools(spec))
        evidence = validate_evidence(extraction, spec)
        audit = step("audit", "data", prompts.AUDIT,
                     {"data": spec["data"], "files": manifest}, team.data_tools(manifest))
        team.validate_audit(audit, manifest, state["calls"]["audit"])
        draft, reviews, errors, previous_ids = {"tasks": [], "gaps": []}, {}, [], None
        if evidence:
            context = dict(base, extraction=extraction, data_audit=audit)
            draft = step("generate", "designer", prompts.GENERATE, context)
            for attempt in range(config["max_revisions"] + 1):
                errors, reviews = [], {}
                try:
                    tasks = validate_tasks(draft, spec, evidence, previous_ids)
                except (ValueError, TypeError, KeyError) as exc:
                    errors = [str(exc)]
                    tasks = {}
                if not errors:
                    previous_ids = list(tasks)
                    if tasks:
                        response = step("review_%d" % attempt, "reviewer", prompts.REVIEW, dict(context, draft=draft))
                        reviews = validate_reviews(response, tasks)
                needs_revision = errors or any(r["verdict"] == "revise" for r in reviews.values())
                if not needs_revision or attempt == config["max_revisions"]:
                    break
                draft = step("revise_%d" % (attempt + 1), "designer", prompts.REVISE,
                             dict(context, draft=draft, errors=errors, reviews=list(reviews.values())))
        records = []
        if not errors:
            for t in draft["tasks"]:
                verdict = reviews[t["id"]]["verdict"]
                status = {"reject": "rejected", "revise": "needs_revision", "pass": "needs_execution"}[verdict]
                if verdict == "pass" and any(not manifest[k]["available"] for k in t["data_ids"]):
                    status = "needs_data"
                if config["demo"]:
                    status = "demo_only"
                records.append(dict(t, status=status, review=reviews[t["id"]],
                                    execution_verified=False, no_data_control_verified=False))
        if config.get("verify"):
            for index, task in enumerate(records):
                if task["status"] != "needs_execution":
                    continue
                history = []
                for attempt in range(config["max_revisions"] + 1):
                    checked = evaluate.verify(task, spec, manifest, step, config["image"],
                                              config.get("execution_timeout", 60),
                                              attempt=attempt)
                    history.append({"attempt": attempt, **checked})
                    task.update(checked)
                    if task["status"] != "needs_revision" or attempt == config["max_revisions"]:
                        break
                    # Route concrete execution feedback back to the designer.
                    revised = step("execution_revise_%s_%d" % (task["id"], attempt), "designer",
                                   prompts.REVISE, dict(context,
                                   draft={"tasks": [task], "gaps": []}, errors=[],
                                   reviews=[checked["evaluation"]]))
                    validate_tasks(revised, spec, evidence, [task["id"]])
                    candidate = revised["tasks"][0]
                    review = step("execution_review_%s_%d" % (task["id"], attempt), "reviewer",
                                  prompts.REVIEW, dict(context, draft=revised))
                    verdict = validate_reviews(review, {candidate["id"]: candidate})[candidate["id"]]
                    task = dict(candidate, review=verdict, execution_verified=False,
                                no_data_control_verified=False, status="needs_revision")
                    if verdict["verdict"] != "pass":
                        task["status"] = "rejected" if verdict["verdict"] == "reject" else "needs_revision"
                        break
                    if any(not manifest[k]["available"] for k in task["data_ids"]):
                        task["status"] = "needs_data"
                        break
                task["verification_history"] = history
                records[index] = task
        packet = {"title": spec["title"], "track": spec["track"], "demo": config["demo"],
                  "benchmark_ready": False, "file_manifest": manifest, "sources": spec["sources"],
                  "evidence": extraction["evidence"], "data_audit": audit, "tasks": records,
                  "gaps": extraction["gaps"] + (draft.get("gaps", []) if isinstance(draft.get("gaps"), list) else []),
                  "validation_errors": errors}
        write_json(out / "tasks.json", packet)
        # Public handoff is opt-in; the evaluator consumes it in memory by default.
        if config.get("export_solver"):
            write_json(out / "solver.json", {"tasks": [
                team.solver_packet(t, spec, manifest) for t in records
                if t["review"]["verdict"] == "pass"]})
        with (out / "tasks.csv").open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["task_id", "status", "question", "data_ids", "evidence_ids", "milestones", "unresolved",
                             "execution_verified", "no_data_control_verified", "review"])
            for t in records:
                row = [t["id"], t["status"], t["question"], ",".join(t["data_ids"]),
                       ",".join(t["evidence_ids"]), len(t["milestones"]), "; ".join(t["unresolved"]),
                       t["execution_verified"], t["no_data_control_verified"],
                       t.get("evaluation", {}).get("feedback", t["review"]["feedback"])]
                writer.writerow(["'" + v if isinstance(v, str) and v.lstrip().startswith(("=", "+", "-", "@")) else v for v in row])
        state["status"] = "needs_revision" if errors else ("no_tasks" if not records else "completed")
        state.pop("error", None)
        write_json(state_path, state)
        return packet
    except Exception as exc:
        state["status"], state["error"] = "failed", type(exc).__name__ + ": " + str(exc)
        write_json(state_path, state)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", nargs="?", type=Path)
    parser.add_argument("--out", type=Path, default=Path("runs/latest"))
    parser.add_argument("--demo", action="store_true", help="offline synthetic fixture; no API calls")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--max-turns", type=int, default=8, help="per-agent turn budget, 1..64")
    parser.add_argument("--role-model", action="append", default=[], metavar="ROLE=MODEL")
    parser.add_argument("--verify", action="store_true", help="run blind solver and no-data control in Docker")
    parser.add_argument("--image", help="trusted preinstalled Python image containing analysis packages")
    parser.add_argument("--execution-timeout", type=float, default=60)
    parser.add_argument("--export-solver", action="store_true", help="also export a public solver handoff")
    parser.add_argument("--max-revisions", type=int, choices=range(3), default=1)
    args = parser.parse_args()
    try:
        require(1 <= args.max_turns <= 64, "max-turns must be 1..64")
        require(args.execution_timeout > 0, "execution-timeout must be positive")
        require(not (args.demo and args.verify), "--demo cannot verify real execution")
        require(args.timeout > 0, "timeout must be positive")
        require(not args.demo or args.input is None, "--demo uses only the bundled synthetic example")
        require(args.demo or args.input is not None, "provide an input JSON or use --demo")
        path = Path(__file__).with_name("example.json") if args.demo else args.input.resolve()
        spec, manifest = load_input(path)
        models = {}
        for item in args.role_model:
            role, separator, model = item.partition("=")
            require(separator and role in team.ROLES and text(model), "use --role-model ROLE=MODEL")
            require(role not in models, "duplicate role-model: " + role)
            models[role] = model
        image = evaluate.DockerRunner.resolve_image(args.image) if args.verify else None
        client = demo_team if args.demo else {
            role: Client(models.get(role, args.model), args.base_url, args.timeout) for role in team.ROLES}

        args.out.mkdir(parents=True, exist_ok=True)
        # A single output directory is one run; never use it concurrently.
        import fcntl
        with (args.out / ".run.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            packet = run(spec, manifest, args.out, client,
                         {"demo": args.demo, "model": "fixture" if args.demo else args.model,
                          "base_url": None if args.demo else args.base_url,
                          "max_revisions": args.max_revisions, "max_turns": args.max_turns,
                          "role_models": models, "verify": args.verify, "image": image,
                          "execution_timeout": args.execution_timeout,
                          "export_solver": args.export_solver})
        print(json.dumps({"out": str(args.out.resolve()), "tasks": len(packet["tasks"]),
                          "benchmark_ready": False, "validation_errors": packet["validation_errors"]}, ensure_ascii=False))
        return 2 if packet["validation_errors"] else 0
    except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
        print("Error: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
