#!/usr/bin/env python3
"""Run lm-eval (0.4.13) against a served model through a chat wrapper suited to thinking models.

Registers model type "spark-chat" = lm-eval's local-chat-completions, except:
  * task stop strings ("until") are NOT sent to the server: on a thinking model they can fire inside the
    reasoning and cut the reply off before any answer exists. The chat template's own end-of-turn ends it.
  * sampling is the same for every model and suite: temperature 0.6, top_p 0.95, seed 1234
    (same as S7; greedy decoding makes thinking models loop).
  * thinking stays on (no enable_thinking flag) unless SPARK_THINKING=off, which sends
    chat_template_kwargs {"enable_thinking": false} (used for the S6 thinking-off comparison).
Scoring uses `content` only; the server's reasoning parser keeps the thinking out of it.

Usage: lmeval_run.py <lm-eval CLI args...>   (run with ~/.venvs/lmeval/bin/python)
"""
import os
import sys

from lm_eval.api.registry import register_model
from lm_eval.models.openai_completions import LocalChatCompletion


@register_model("spark-chat")
class SparkChat(LocalChatCompletion):
    def _create_payload(self, messages, generate=False, gen_kwargs=None, seed=1234, eos=None, **kwargs):
        gen_kwargs = dict(gen_kwargs or {})
        gen_kwargs.pop("until", None)
        for k in ("temperature", "top_p", "top_k", "do_sample"):
            gen_kwargs.pop(k, None)
        payload = super()._create_payload(messages, generate=generate, gen_kwargs=gen_kwargs, seed=seed, eos=None, **kwargs)
        payload.pop("stop", None)
        payload.update(temperature=0.6, top_p=0.95, seed=1234)
        if os.environ.get("SPARK_THINKING") == "off":
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        return payload

    def parse_generations(self, outputs, **kwargs):
        # After the reasoning is split off, the answer usually starts with "\n\n". The MMLU *_generative
        # filter keeps only the text before the first newline, so an untrimmed "\n\nC" scored as "" (wrong).
        return [g.lstrip() if isinstance(g, str) else g for g in super().parse_generations(outputs, **kwargs)]


if __name__ == "__main__":
    from lm_eval.__main__ import cli_evaluate
    sys.argv = ["lm-eval"] + sys.argv[1:]
    cli_evaluate()
