"""Blind solver execution and evaluation. Generated code runs only in Docker."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import uuid

import prompts
from team import Tool, solver_packet


def check_artifact(name):
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", name):
        raise ValueError("artifact must be a simple relative filename")
    return name


class DockerRunner:
    """Trusted preinstalled image; per-task workspace; no network or author files."""
    def __init__(self, image, files, work, timeout=60):
        self.image, self.files, self.work, self.timeout = image, files, Path(work), timeout
        self.work.mkdir(parents=True, exist_ok=True)
        self.receipts = []

    @staticmethod
    def resolve_image(image):
        if not image or image.startswith("-"):
            raise ValueError("provide a trusted preinstalled --image")
        try:
            found = subprocess.run(["docker", "image", "inspect", image, "--format", "{{.Id}}"],
                                   capture_output=True, text=True, timeout=20, check=True)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("Docker or local image unavailable; no host execution fallback") from exc
        digest = found.stdout.strip()
        if not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
            raise RuntimeError("could not resolve Docker image digest")
        return digest

    def command(self, name):
        cmd = ["docker", "run", "--rm", "--pull=never", "--name", name, "-i",
               "--network=none", "--read-only", "--cap-drop=ALL",
               "--security-opt=no-new-privileges", "--pids-limit=128",
               "--memory=4g", "--cpus=2", "--ulimit", "fsize=16777216:16777216",
               "--log-driver=none", "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m",
               "--user", "%s:%s" % (os.getuid(), os.getgid()),
               "--env", "HOME=/tmp", "--env", "PYTHONDONTWRITEBYTECODE=1",
               "--workdir", "/work"]
        mounts = [(self.work.resolve(), "/work", False)] + [
            (Path(host).resolve(), inside, True) for host, inside in self.files]
        for host, inside, readonly in mounts:
            if "," in str(host):
                raise ValueError("Docker mount paths cannot contain commas")
            mount = "type=bind,src=%s,dst=%s" % (host, inside)
            cmd += ["--mount", mount + (",readonly" if readonly else "")]
        return cmd + ["--entrypoint", "python", self.image, "-"]

    def execute(self, code):
        if not isinstance(code, str) or not 1 <= len(code) <= 40000:
            raise ValueError("python code must be 1..40000 characters")
        name = "omics-" + uuid.uuid4().hex
        command = self.command(name)
        # Bound captured output in memory; stop a noisy child before filling disk.
        import threading
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT)
        output, overflow = bytearray(), threading.Event()

        def collect():
            while True:
                block = process.stdout.read(4096)
                if not block:
                    break
                room = 65536 - len(output)
                output.extend(block[:max(0, room)])
                if len(block) > room:
                    overflow.set()
                    process.kill()
                    break
        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        timed_out = False
        try:
            process.stdin.write(code.encode())
            process.stdin.close()
            process.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.wait()
        except BrokenPipeError:
            process.wait(timeout=self.timeout)
        finally:
            # Also remove a container that survived a killed docker client.
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
            reader.join(timeout=5)
        receipt = {"code": code, "code_sha256": hashlib.sha256(code.encode()).hexdigest(),
                   "exit_code": process.returncode, "timed_out": timed_out,
                   "output_truncated": overflow.is_set(),
                   "stdout": bytes(output).decode("utf-8", errors="replace")}
        self.receipts.append(receipt)
        return receipt

    def artifact(self, name):
        check_artifact(name)
        path = self.work / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("artifact missing or not a regular file")
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("artifact exceeds 16 MiB limit")
        value = path.read_bytes()
        return {"name": name, "bytes": len(value), "sha256": hashlib.sha256(value).hexdigest(),
                "preview": value[:12000].decode("utf-8", errors="replace"),
                "preview_truncated": len(value) > 12000}

    def tools(self):
        return {"run_python": Tool(
            "Execute Python in the isolated task container. Arguments: code. "
            "Inputs under /data, persist outputs under /work. No network.", self.execute),
            "read_artifact": Tool("Read a generated output. Arguments: name (simple filename).", self.artifact)}


def validate_solution(value):
    if value.get("status") not in ("completed", "insufficient_data", "failed"):
        raise ValueError("invalid solver status")
    if not isinstance(value.get("answer"), str) or not value["answer"].strip():
        raise ValueError("solver must explain its answer or limitation")
    if not isinstance(value.get("artifacts"), list):
        raise ValueError("solver must list artifacts")
    for name in value["artifacts"]:
        check_artifact(name)
    if len(set(value["artifacts"])) != len(value["artifacts"]):
        raise ValueError("duplicate solver artifacts")
    return value


def validate_grade(value):
    if value.get("verdict") not in ("supported", "revise", "reject"):
        raise ValueError("invalid evaluation verdict")
    for field in ("execution_supported", "data_dependence_supported"):
        if type(value.get(field)) is not bool:
            raise ValueError("evaluation needs boolean " + field)
    if not isinstance(value.get("feedback"), str) or not value["feedback"].strip():
        raise ValueError("evaluation needs feedback")
    if value["verdict"] == "supported" and not (
            value["execution_supported"] and value["data_dependence_supported"]):
        raise ValueError("supported verdict contradicts evaluation checks")
    return value


def verify(task, spec, manifest, step, image, timeout=60, runner_factory=DockerRunner, attempt=0):
    """Fresh solver session and workspace for each condition. No data leaves via network."""
    public = solver_packet(task, spec, manifest)
    for milestone in task["milestones"]:
        check_artifact(milestone["artifact"])
    trials = {}
    # Temp workspaces are outside the author run directory and never contain gold.
    for condition in ("with_data", "without_data"):
        key = "execution_%s_%d_%s" % (task["id"], attempt, condition)
        cached = step.bundle(key)
        if cached is not None:
            saved = step.artifact_dir(key)
            for artifact in cached["artifacts"]:
                target = saved / check_artifact(artifact["name"])
                if (target.is_symlink() or not target.is_file() or
                        hashlib.sha256(target.read_bytes()).hexdigest() != artifact["sha256"]):
                    raise RuntimeError("cached execution artifact missing or changed")
            trials[condition] = cached
            continue
        if step.started(key):
            raise RuntimeError("interrupted execution lost its temporary workspace; use a new output directory")
        with tempfile.TemporaryDirectory(prefix="omics-solver-") as folder:
            files = [(manifest[d["id"]]["path"], d["path"]) for d in public["data"]]
            runner = runner_factory(image, files if condition == "with_data" else [], folder, timeout)
            payload = json.loads(json.dumps(public))
            if condition == "without_data":
                payload["data"] = [{**d, "available": False} for d in public["data"]]
            # The model sees only the public task; no extraction, gold or draft reviews.
            solution = step(key, "solver", prompts.SOLVE, payload, runner.tools())
            validate_solution(solution)
            # Artifacts are captured into the checkpoint before deleting the workspace.
            # A completed evaluation resumes from this bundle, not from missing temp files.
            def capture():
                artifacts = [runner.artifact(n) for n in solution["artifacts"]]
                destination = step.artifact_dir(key)
                destination.mkdir(parents=True, exist_ok=True)
                for artifact in artifacts:
                    target = destination / artifact["name"]
                    if target.is_symlink():
                        raise ValueError("refusing symlink in artifact destination")
                    target.write_bytes((runner.work / artifact["name"]).read_bytes())
                return {"solution": solution, "receipts": runner.receipts, "artifacts": artifacts}
            trials[condition] = step.capture(key, capture)
    grading = step("grade_%s_%d" % (task["id"], attempt), "reviewer", prompts.EVALUATE,
                   {"task": task, "trials": trials})
    validate_grade(grading)
    real, control = trials["with_data"], trials["without_data"]
    artifacts = {a["name"]: a for a in real["artifacts"]}
    expected = {m["artifact"] for m in task["milestones"]}
    ran = any(r["exit_code"] == 0 and not r["timed_out"] and not r["output_truncated"]
              for r in real["receipts"])
    complete = (real["solution"]["status"] == "completed" and ran and
                expected <= artifacts.keys() and all(artifacts[n]["bytes"] for n in expected))
    # This is evidence for data dependence, not proof; a failed control is inconclusive.
    dependence = (control["solution"]["status"] == "insufficient_data" and
                  grading["data_dependence_supported"])
    execution = bool(complete and grading["execution_supported"])
    status = "needs_human_review" if execution and dependence and grading["verdict"] == "supported" else (
        "rejected" if grading["verdict"] == "reject" else "needs_revision")
    return {"status": status, "execution_verified": execution,
            "no_data_control_verified": dependence, "evaluation": grading, "trials": trials}
