#!/usr/bin/env python3
"""Re-score lm-eval samples with a format-tolerant answer extractor (S2/S6).

Why: lm-eval's regex filters were written for base models that copy an exact answer pattern. Chat/thinking
models answer correctly but in their own format (\\boxed{D}, "Answer: C", "option (B)", "**False**"), which
the stock filters score as wrong or [invalid]. Run 2's smoke test had 7 correct answers scored wrong that way.
We report BOTH lm-eval's own metric and this rescored accuracy; summary.md says which is which.

Only multiple-choice / short-answer tasks are rescored (MMLU generative, MMLU-Pro, BBH, GPQA).
IFEval has its own rule-based checker and is reported as lm-eval scores it.

Usage: lmeval_rescore.py <dir with samples_*.jsonl (searched recursively)> [--show-misses N]
Writes rescore.json next to the samples and prints a table.
"""
import glob
import json
import os
import re
import sys

ANSWER_PHRASES = (r"final answer|the answer is|answer is|answer\s*:|correct (?:choice|answer|option) is|"
                  r"corresponds to (?:option|choice|answer)?|choose|option is")


# BBH one-word label targets: an answer that starts with the label and then explains is accepted.
LABELS = {"yes", "no", "true", "false", "valid", "invalid"}


def clean(text):
    t = text.replace("\u202f", " ").replace("\u00a0", " ")
    t = re.sub(r"\\text\{([^{}]*)\}", r"\1", t)
    t = re.sub(r"[*_`]+", "", t)                       # markdown bold/italic/code
    t = re.sub(r"\\\(\s*(.*?)\s*\\\)", r"\1", t)          # inline LaTeX \(-80\) -> -80
    return t.strip()


def candidates(text):
    """Answer spans, most authoritative first: last \\boxed{}, then last 'answer is ...' phrase, then last line."""
    t = clean(text)
    out = []
    boxed = re.findall(r"\\boxed\{\s*((?:[^{}]|\{[^{}]*\})*?)\s*\}", t)
    if boxed:
        out.append(boxed[-1])
    phr = list(re.finditer(rf"(?i)(?:{ANSWER_PHRASES})\s*[:\-]?\s*([^\n]+)", t))
    if phr:
        out.append(phr[-1].group(1))
    lines = [l.strip() for l in t.splitlines() if l.strip() and not re.fullmatch(r"\\\[|\\\]|\$\$?|[-= ]+", l.strip())]  # LaTeX/rule lines, not "] ]"
    if lines:
        out.append(lines[-1])
    # Last resort: an answer stated up front, then explained ("**C. Whitebox**", "## Answer: **A**"). Only the first
    # line, and only a letter followed by "." / ")" or alone, so neither "A buffer ..." nor a restated option list
    # deep inside a long, unfinished reasoning counts.
    if lines and re.match(r"^(?:#+\s*)?(?i:(?:final\s+)?answer\s*:?\s*)?\(?[A-R](?:[.)](?:\s|$)|\s*$)", lines[0]):
        out.append(lines[0])
    return out


def letter_of(span, letters):
    m = re.findall(rf"\(([{letters}])\)", span)                 # "(C)" anywhere -> last
    if m:
        return m[-1]
    span = re.sub(r"^(?:#+\s*)?(?:(?:final\s+)?answer\s*:?\s*)", "", span, flags=re.I)   # heading-style lead-in
    m = re.match(rf"^\s*([{letters}])(?:[\s.):,]|$)", span)      # starts with the letter
    if m:
        return m.group(1)
    m = re.findall(rf"\b(?:option|choice)\s+([{letters}])\b", span, re.I)
    if m:
        return m[-1].upper()
    return None


def norm(s):
    s = clean(str(s)).lower()
    s = re.sub(r"^[\s(\"']+|[\s).\"'!]+$", "", s)
    return re.sub(r"\s+", " ", s)


def judge(task, target, response):
    """Return (correct: bool, extracted: str|None)."""
    tgt = str(target).strip()
    cands = candidates(response or "")
    if not cands:
        return False, None
    m = re.fullmatch(r"\(?([A-R])\)?", tgt)
    if m:                                                        # multiple-choice letter target
        # MMLU-Pro has 10 options; BBH goes up to (K) (geometric_shapes) and (R) (reasoning_about_colored_objects)
        letters = "A-J" if "mmlu_pro" in task else "A-R" if task.startswith("bbh") else "A-H"
        for c in cands:
            got = letter_of(c, letters)
            if got:
                return got == m.group(1), got
        return False, None
    t = norm(tgt)                                                 # free-form target (BBH)
    if t in LABELS:                                               # yes/no-style: the first candidate that states
        alt = "|".join(sorted(LABELS))                            # a label decides ("No, Elanor does not ...")
        for c in cands:
            n = norm(c)
            m = re.match(rf"({alt})\b(?!-)", n) or re.search(rf"(?:^|[\s:=])({alt})$", n)
            if m:
                return m.group(1) == t, m.group(1)
        return False, norm(cands[0])
    for c in cands:
        n = norm(c)
        if "," not in t:                                          # word_sorting answered as "a, b, c"
            n = re.sub(r"\s*,\s*", " ", n)
        if "dyck" in task and n.replace(" ", "") == t.replace(" ", ""):   # "]]" for target "] ]"
            return True, c
        if n == t:
            return True, c
        if re.search(rf"(?:^|[\s:=])({re.escape(t)})$", n):      # "... the result is false" / "... is 12"
            return True, c
    return False, norm(cands[0])


def main():
    root = sys.argv[1]
    show = int(sys.argv[sys.argv.index("--show-misses") + 1]) if "--show-misses" in sys.argv else 0
    report = {}
    newest = {}                                   # a resumed run writes a second, timestamped set: keep the newest
    for f in sorted(glob.glob(os.path.join(root, "**", "samples_*.jsonl"), recursive=True)):
        task = re.sub(r"_\d{4}-\d\d-\d\dT.*$", "", os.path.basename(f)[len("samples_"):])
        if task not in newest or os.path.basename(f) > os.path.basename(newest[task]):
            newest[task] = f
    for task, f in sorted(newest.items()):
        if task.startswith("ifeval"):
            continue
        n = ok = noans = stock = 0; misses = []; seen = set()
        for line in open(f):
            d = json.loads(line)
            if d.get("doc_id") in seen:      # lm-eval logs a sample once per filter (e.g. GPQA strict + flexible)
                continue
            seen.add(d.get("doc_id"))
            resp = d["resps"][0][0] if d.get("resps") else ""
            good, got = judge(task, d.get("target"), resp)
            n += 1; ok += good; noans += got is None
            stock += float(d.get("exact_match", 0) or 0)
            if not good and len(misses) < show:
                misses.append((d.get("target"), got, resp[-200:]))
        report[task] = {"n": n, "rescored_acc": round(ok / n, 4) if n else None, "no_answer": noans,
                        "stock_acc_first_filter": round(stock / n, 4) if n else None}
        for tg, got, tail in misses:
            print(f"  MISS {task}: target={tg!r} got={got!r} tail={tail!r}")
    json.dump(report, open(os.path.join(root, "rescore.json"), "w"), indent=1)
    print(f"{'task':55s} {'n':>5s} {'rescored':>9s} {'stock':>7s} {'no-ans':>7s}")
    for t, r in report.items():
        print(f"{t:55s} {r['n']:5d} {r['rescored_acc']:9.3f} {r['stock_acc_first_filter']:7.3f} {r['no_answer']:7d}")


if __name__ == "__main__":
    main()
