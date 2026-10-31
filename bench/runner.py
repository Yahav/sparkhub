#!/usr/bin/env python3
"""SparkHub benchmark runner. Runs inside the sparkhub-bench container, started by the hub.

  runner.py run       BENCH_CONFIG (JSON) and OPENAI_API_KEY (the run's key) in the environment
  runner.py prepare   download the lm-eval datasets into $HF_HOME (the installer runs it; runs are offline)

Suites: tb2-quick, tb2-full (Terminal-Bench 2.0 through Harbor, opencode as the agent), s2 (knowledge), s6 (BBH
reasoning), s6-nothink (BBH, thinking off). Everything goes to the model through the hub's gateway with the
run's key. Progress goes to <out>/progress.json, results to <out>/results.json; the hub reads both.

Harbor drives the host's Docker through the socket, and Docker resolves bind mounts on the host: <out> and the
Terminal-Bench copy are mounted at their host paths, so the paths Harbor sees are valid on the host too.
"""
import glob
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

RUNNER_VERSION = "1"
OPENCODE_VERSION = "1.18.35"   # npm's latest 1.x; Harbor's opencode agent uses the 1.x CLI
LMEVAL = "/opt/lmeval/bin/python"
TASK_CA = "/usr/local/share/sparkhub-ca.crt"   # the hub's CA inside each task image, for opencode (Node)
S2_TASKS = ("mmlu_pro_computer_science,mmlu_pro_engineering,mmlu_computer_security_generative,"
            "mmlu_college_computer_science_generative,ifeval")
S6_TASKS = "bbh_cot_fewshot"
LMEVAL_SUITES = {  # suite: (tasks, --limit, questions (for progress), thinking)
    "s2": (S2_TASKS, 200, 800, True),
    "s6": (S6_TASKS, 50, 1311, True),
    "s6-nothink": (S6_TASKS, 50, 1311, False),
}
TB2_LISTS = {"tb2-quick": "/bench/tb2-quick.txt", "tb2-full": "/bench/tb2-full.txt"}

_child: subprocess.Popen | None = None
_stopping = False


def _on_term(signum, frame):
    """The hub cancels by stopping the container: let Harbor tear its task containers down."""
    global _stopping
    _stopping = True
    if _child and _child.poll() is None:
        _child.send_signal(signal.SIGINT)


def write_json(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class Progress:
    def __init__(self, out: str, suites: list[str]):
        self.path = os.path.join(out, "progress.json")
        self.state = {"runner_version": RUNNER_VERSION, "suites": suites, "done": [], "suite": None,
                      "n_done": 0, "n_total": 0, "detail": "", "updated_at": time.time()}

    def set(self, **kw) -> None:
        self.state.update(kw, updated_at=time.time())
        write_json(self.path, self.state)


def run_child(cmd: list[str], log_path: str, env: dict | None = None, poll=None) -> int:
    """Run a command, its output to a log file; `poll` is called every 10 s while it runs."""
    global _child
    with open(log_path, "a") as log:
        line = " ".join(cmd)
        key = os.environ.get("OPENAI_API_KEY")
        log.write(f"\n$ {line.replace(key, 'shk_…') if key else line}\n")     # the run's key stays out of the log
        log.flush()
        _child = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, env={**os.environ, **(env or {})})
        while _child.poll() is None:
            if poll:
                try:
                    poll()
                except Exception as e:      # progress is best effort
                    log.write(f"[runner] progress: {e}\n")
            try:
                _child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        rc = _child.returncode
        _child = None
        return rc


# ------------------------------------------------------------------ Terminal-Bench (Harbor)
def task_names(list_path: str) -> list[str]:
    return [ln.split("\t")[0].strip() for ln in open(list_path) if ln.strip() and not ln.startswith("#")]


def prepare_tasks(cfg: dict, suite: str, dst: str) -> list[str]:
    """Copy the suite's tasks. Each image gets the hub's CA (opencode verifies the gateway's certificate), and
    without internet the agent phase may reach only the gateway (Harbor's egress allowlist)."""
    names = task_names(TB2_LISTS[suite])
    if cfg.get("only_tasks"):          # testing the setup: a few tasks instead of the suite
        names = [n for n in names if n in cfg["only_tasks"]]
    if os.path.exists(dst):
        shutil.rmtree(dst)
    os.makedirs(dst)
    for name in names:
        src = os.path.join(cfg["tb2_dir"], name)
        if not os.path.isdir(src):
            raise RuntimeError(f"Terminal-Bench task {name} missing in {cfg['tb2_dir']}; run ./install.sh")
        task = os.path.join(dst, name)
        shutil.copytree(src, task, symlinks=True)
        env_dir = os.path.join(task, "environment")
        shutil.copyfile(cfg["ca"], os.path.join(env_dir, "sparkhub-ca.crt"))
        with open(os.path.join(env_dir, "Dockerfile"), "a") as f:
            f.write(f"\n# added by SparkHub's benchmark runner: the hub's CA, for the agent's connection to it\n"
                    f"COPY sparkhub-ca.crt {TASK_CA}\n")
        toml = os.path.join(task, "task.toml")
        lines = [ln for ln in open(toml).read().splitlines() if not ln.startswith("docker_image")]  # amd64 only
        if not cfg["internet"]:
            policy = ['network_mode = "allowlist"', f"allowed_hosts = {json.dumps(cfg['allowed_hosts'])}"]
            if "[agent]" in lines:
                i = lines.index("[agent]")
                lines = lines[:i + 1] + policy + lines[i + 1:]
            else:
                lines += ["", "[agent]", *policy]
        open(toml, "w").write("\n".join(lines) + "\n")
    return names


def _seconds(span: dict) -> int | None:
    try:
        return round((datetime.fromisoformat(span["finished_at"].replace("Z", "+00:00")) -
                      datetime.fromisoformat(span["started_at"].replace("Z", "+00:00"))).total_seconds())
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def tb2_results(job_dir: str) -> dict:
    tasks = {}
    for path in glob.glob(os.path.join(job_dir, "*", "result.json")):
        try:
            r = json.load(open(path))
        except ValueError:
            continue
        name = str(r.get("task_name") or "").split("/")[-1] or os.path.basename(os.path.dirname(path)).split("__")[0]
        reward = ((r.get("verifier_result") or {}).get("rewards") or {}).get("reward")
        exc = r.get("exception_info") or None
        tasks[name] = {"reward": reward, "agent_s": _seconds(r.get("agent_execution") or {}),
                       "error": (exc.get("exception_type") or "error") if isinstance(exc, dict) else None}
    solved = sum(1 for t in tasks.values() if t["reward"] == 1.0)
    return {"solved": solved, "total": len(tasks), "tasks": tasks}


def run_tb2(cfg: dict, suite: str, prog: Progress) -> dict:
    out = cfg["out"]
    tasks_dir = os.path.join(out, f"tasks-{suite}")
    names = prepare_tasks(cfg, suite, tasks_dir)
    jobs = os.path.join(out, "jobs")
    os.makedirs(jobs, exist_ok=True)
    job = suite
    if os.path.exists(os.path.join(jobs, job)):
        shutil.rmtree(os.path.join(jobs, job))
    model = cfg["model"]
    oc = {"provider": {"hub": {"npm": "@ai-sdk/openai-compatible", "name": "SparkHub",
                               "options": {"baseURL": cfg["base_url"], "apiKey": os.environ["OPENAI_API_KEY"]},
                               "models": {model: {"name": model, "limit": {"context": cfg["context"],
                                                                           "output": cfg["max_output"]}}}}}}
    cmd = ["harbor", "run", "-p", tasks_dir, "-a", "opencode", "-m", f"hub/{model}",
           "--ak", f"version={OPENCODE_VERSION}", "--ak", f"opencode_config={json.dumps(oc)}",
           "--ae", f"NODE_EXTRA_CA_CERTS={TASK_CA}",
           "-n", str(cfg["n_concurrent"]), "-k", "1", "-o", jobs, "--job-name", job, "-y", "-q"]

    def poll():
        res = tb2_results(os.path.join(jobs, job))
        prog.set(n_done=res["total"], n_total=len(names), detail=f"{res['solved']} solved so far")

    prog.set(suite=suite, n_done=0, n_total=len(names), detail="building task environments")
    rc = run_child(cmd, os.path.join(out, f"{suite}.log"), poll=poll)
    res = tb2_results(os.path.join(jobs, job))
    res.update(exit_code=rc, n_tasks=len(names), internet=cfg["internet"])
    if rc != 0 and not res["tasks"]:
        raise RuntimeError(f"harbor exited {rc} without results; see {suite}.log")
    return res


# ------------------------------------------------------------------ lm-eval
def _cache_rows(cache_prefix: str) -> int:
    n = 0
    for db in glob.glob(cache_prefix + "*"):
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as c:
                n += c.execute("SELECT COUNT(*) FROM unnamed").fetchone()[0]
        except sqlite3.Error:
            pass
    return n


def lmeval_results(out_dir: str) -> dict:
    res: dict = {"tasks": {}}
    rescore = os.path.join(out_dir, "rescore.json")
    if os.path.exists(rescore):
        for task, r in json.load(open(rescore)).items():
            res["tasks"][task] = {"n": r.get("n"), "accuracy": r.get("rescored_acc"),
                                  "stock_accuracy": r.get("stock_acc_first_filter"), "no_answer": r.get("no_answer")}
    for path in glob.glob(os.path.join(out_dir, "**", "results_*.json"), recursive=True):
        data = json.load(open(path)).get("results", {})
        if "ifeval" in data:
            r = data["ifeval"]
            res["tasks"]["ifeval"] = {"accuracy": r.get("prompt_level_strict_acc,none"),
                                      "loose_accuracy": r.get("prompt_level_loose_acc,none")}
    accs = [t["accuracy"] for t in res["tasks"].values() if isinstance(t.get("accuracy"), (int, float))]
    res["mean_accuracy"] = round(sum(accs) / len(accs), 4) if accs else None
    return res


def run_lmeval(cfg: dict, suite: str, prog: Progress) -> dict:
    tasks, limit, total, thinking = LMEVAL_SUITES[suite]
    if cfg.get("limit"):               # testing the setup: a few questions per task
        limit, total = cfg["limit"], cfg["limit"] * len(tasks.split(","))
    out_dir = os.path.join(cfg["out"], suite)
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(cfg["cache_dir"], exist_ok=True)
    cache = os.path.join(cfg["cache_dir"], suite)      # per model: a cancelled run resumes from its answers
    args = (f"model={cfg['model']},base_url={cfg['base_url']}/chat/completions,num_concurrent={cfg['n_concurrent']},"
            f"max_retries=3,timeout=3600,tokenized_requests=False,max_gen_toks=16384")
    cmd = [LMEVAL, "/bench/lmeval_run.py", "run", "--model", "spark-chat", "--model_args", args, "--tasks", tasks,
           "--limit", str(limit), "--apply_chat_template", "--gen_kwargs", "max_gen_toks=16384",
           "--output_path", out_dir, "--log_samples", "--use_cache", cache]
    env = {"SSL_CERT_FILE": cfg["ca"], "REQUESTS_CA_BUNDLE": cfg["ca"], "HF_DATASETS_OFFLINE": "1",
           "HF_HUB_OFFLINE": "1", "SPARK_THINKING": "on" if thinking else "off"}
    prog.set(suite=suite, n_done=_cache_rows(cache), n_total=total, detail="")
    log = os.path.join(cfg["out"], f"{suite}.log")
    rc = run_child(cmd, log, env=env, poll=lambda: prog.set(n_done=min(_cache_rows(cache), total)))
    if rc != 0:
        raise RuntimeError(f"lm-eval exited {rc}; see {suite}.log")
    run_child([LMEVAL, "/bench/lmeval_rescore.py", out_dir], log)
    res = lmeval_results(out_dir)
    res["thinking"] = thinking
    return res


# ------------------------------------------------------------------ main
def run(cfg: dict) -> int:
    out = cfg["out"]
    os.makedirs(out, exist_ok=True)
    results_path = os.path.join(out, "results.json")
    results = {"runner_version": RUNNER_VERSION, "suites": {}}
    prog = Progress(out, cfg["suites"])
    prog.set()
    failed = False
    for suite in cfg["suites"]:
        if _stopping:
            break
        t0 = time.time()
        try:
            res = run_tb2(cfg, suite, prog) if suite in TB2_LISTS else run_lmeval(cfg, suite, prog)
        except Exception as e:
            failed = True
            res = {"error": str(e)[:500]}
        res["seconds"] = round(time.time() - t0)
        if _stopping:
            res["cancelled"] = True
        results["suites"][suite] = res
        write_json(results_path, results)
        prog.set(done=prog.state["done"] + [suite])
    prog.set(suite=None, detail="cancelled" if _stopping else "finished")
    return 2 if _stopping else (1 if failed else 0)


def prepare() -> int:
    """Download the datasets of every lm-eval suite (the installer runs this with network; runs are offline)."""
    tasks = sorted({t for v in LMEVAL_SUITES.values() for t in v[0].split(",")})
    # NLTK first: importing IFEval otherwise downloads its tokenizer to ~/nltk_data, which the container loses
    code = ("import nltk, os\n"
            "for p in ('punkt', 'punkt_tab'):\n"
            "    nltk.download(p, quiet=True, download_dir=os.environ['NLTK_DATA'])\n"
            "from lm_eval.tasks import TaskManager\n"
            f"for name in TaskManager().load_task_or_group({tasks!r}):\n"
            "    print('ready', name, flush=True)\n")
    return subprocess.run([LMEVAL, "-c", code]).returncode


def main() -> int:
    signal.signal(signal.SIGTERM, _on_term)
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "run":
        return run(json.loads(os.environ["BENCH_CONFIG"]))
    if cmd == "prepare":
        return prepare()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
