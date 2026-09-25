#!/usr/bin/env python3
"""NexAgent training runner — runs in the *training* Python environment
(torch + transformers + peft [+ bitsandbytes for QLoRA]), as a separate process.

Modes
  preflight --model DIR --workdir DIR [--recipe R]   → JSON checks on stdout
  train     --job DIR                                → metrics.jsonl, status.json, adapter/
  evaluate  --job DIR --heldout FILE --out FILE      → generations of base and base+adapter
  merge     --job DIR --out DIR                      → merged full model for serving

It never downloads anything (HF_HUB_OFFLINE=1), never executes generated code,
and reports real metrics only. Uses only standard library at import time so
`preflight` can explain exactly which package is missing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import sys
import time
import traceback
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_DATASETS_OFFLINE", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

TEST_STEP_DELAY = float(os.environ.get("NX_TEST_STEP_DELAY_S", "0") or 0)

RECIPES = {
    "lora-sft": {"method": "lora", "r": 16, "alpha": 32, "dropout": 0.05, "lr": 2e-4, "epochs": 3,
                 "batch_size": 1, "grad_accum": 8, "max_len": 2048, "needs_cuda": False},
    "qlora-sft": {"method": "qlora", "r": 16, "alpha": 32, "dropout": 0.05, "lr": 2e-4, "epochs": 3,
                  "batch_size": 1, "grad_accum": 8, "max_len": 2048, "needs_cuda": True},
    "lora-smoke-test": {"method": "lora", "r": 4, "alpha": 8, "dropout": 0.0, "lr": 5e-4, "epochs": 1,
                        "batch_size": 1, "grad_accum": 1, "max_len": 256, "needs_cuda": False},
}
BOUNDS = {"r": (2, 128), "alpha": (2, 256), "lr": (1e-6, 1e-3), "epochs": (1, 20), "batch_size": (1, 16),
          "grad_accum": (1, 64), "max_len": (128, 8192), "dropout": (0.0, 0.5)}
SUPPORTED_TYPES = {"llama", "mistral", "qwen2", "qwen3", "gemma", "gemma2", "gemma3", "phi3", "phi", "gpt2",
                   "gpt_neox", "falcon", "granite", "olmo", "olmo2", "mixtral", "stablelm", "starcoder2"}
SYSTEM = ("You are a careful refinery assistant. Answer only from the numbered sources. Cite every "
          "plant-specific statement with its source number like [1]. Keep equipment tags, numbers, units and "
          "conditions exactly as written. If the sources do not contain the answer, reply exactly: "
          "\"The answer was not found in the accessible sources.\" Answer in the language of the question.")


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    tmp.replace(path)


def file_hashes(root: Path) -> dict[str, str]:
    out = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            h = hashlib.sha256()
            with p.open("rb") as fh:
                for block in iter(lambda: fh.read(1 << 20), b""):
                    h.update(block)
            out[str(p.relative_to(root))] = h.hexdigest()
    return out


def mem_info() -> dict:
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            if k in ("MemTotal", "MemAvailable"):
                info[k] = int(v.split()[0]) * 1024
    except OSError:
        pass
    return info


def software() -> dict:
    out = {"python": platform.python_version()}
    for mod in ("torch", "transformers", "peft", "accelerate", "bitsandbytes", "safetensors", "tokenizers"):
        try:
            out[mod] = __import__(mod).__version__
        except Exception:  # noqa: BLE001
            out[mod] = None
    return out


def hardware() -> dict:
    hw = {"platform": platform.platform(), "cpu_count": os.cpu_count(), **mem_info(), "gpus": []}
    try:
        import torch
        if torch.cuda.is_available():
            for i in range(torch.cuda.device_count()):
                free, total = torch.cuda.mem_get_info(i)
                hw["gpus"].append({"index": i, "name": torch.cuda.get_device_name(i),
                                   "vram_total": total, "vram_free": free})
    except Exception:  # noqa: BLE001
        pass
    return hw


def format_example(tokenizer, ex: dict, max_len: int, with_answer: bool = True):
    sources = "\n".join(f"<source ref=\"{e['ref']}\" file=\"{e['filename']}\" section=\"{e['section']}\">\n"
                        f"{e['text']}\n</source>" for e in ex.get("evidence") or [])
    user = f"SOURCES:\n{sources or '(no sources available)'}\n\nQUESTION:\n{ex['question']}"
    msgs = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}]
    if getattr(tokenizer, "chat_template", None):
        prompt = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=True)
    else:
        prompt = f"### System\n{SYSTEM}\n### User\n{user}\n### Assistant\n"
    if not with_answer:
        return prompt
    p_ids = tokenizer(prompt, add_special_tokens=False)["input_ids"]
    a_ids = tokenizer(ex["answer"] + (tokenizer.eos_token or ""), add_special_tokens=False)["input_ids"]
    ids = (p_ids + a_ids)[-max_len:]
    labels = ([-100] * len(p_ids) + a_ids)[-max_len:]
    return {"input_ids": ids, "labels": labels, "attention_mask": [1] * len(ids)}


# ------------------------------------------------------------------ preflight
def preflight(model_dir: Path, workdir: Path, recipe: str, examples: int) -> dict:
    checks = []

    def add(name, ok, detail, blocking=True):
        checks.append({"name": name, "ok": bool(ok), "detail": detail, "blocking": blocking})

    sw = software()
    add("recipe_allowlisted", recipe in RECIPES, f"recipe '{recipe}'" + ("" if recipe in RECIPES else " is not allow-listed"))
    for mod in ("torch", "transformers", "peft", "accelerate"):
        add(f"package_{mod}", sw.get(mod) is not None,
            f"{mod} {sw.get(mod)}" if sw.get(mod) else f"Install {mod} in the training environment")
    spec = RECIPES.get(recipe, {})
    hw = hardware()
    cuda = bool(hw["gpus"])
    if spec.get("needs_cuda"):
        add("cuda_gpu", cuda, "CUDA GPU found" if cuda else "QLoRA needs an NVIDIA GPU with CUDA; none is visible "
                                                            "(check NEXAGENT_TRAIN_GPU / CUDA_VISIBLE_DEVICES)")
        add("package_bitsandbytes", sw.get("bitsandbytes") is not None,
            f"bitsandbytes {sw.get('bitsandbytes')}" if sw.get("bitsandbytes") else "Install bitsandbytes for QLoRA")
    else:
        add("cuda_gpu", cuda, "CUDA GPU found" if cuda else "No GPU: CPU training will be very slow "
                                                            "(only practical for the smoke-test recipe)", blocking=False)
    files = {p.name for p in model_dir.glob("*")} if model_dir.is_dir() else set()
    add("model_directory", model_dir.is_dir(), str(model_dir) if model_dir.is_dir() else f"{model_dir} does not exist")
    add("model_config", "config.json" in files, "config.json present" if "config.json" in files else "config.json missing")
    tok = {"tokenizer.json", "tokenizer.model", "vocab.json", "tokenizer_config.json"} & files
    add("tokenizer_files", bool(tok), ", ".join(sorted(tok)) or "No tokenizer files found")
    weights = [f for f in files if f.endswith((".safetensors", ".bin")) and "adapter" not in f]
    add("model_weights", bool(weights), f"{len(weights)} weight file(s)" if weights else "No *.safetensors/*.bin weights")
    lic = [f for f in files if f.upper().startswith(("LICENSE", "LICENCE", "NOTICE", "USE_POLICY"))]
    add("license_file", bool(lic), ", ".join(lic) or "No LICENSE file in the model folder — confirm licensing", blocking=False)
    params_est = None
    if "config.json" in files:
        try:
            cfg = json.loads((model_dir / "config.json").read_text())
            mtype = cfg.get("model_type")
            add("architecture_supported", mtype in SUPPORTED_TYPES, f"model_type={mtype}")
            h = cfg.get("hidden_size") or cfg.get("n_embd") or 0
            layers = cfg.get("num_hidden_layers") or cfg.get("n_layer") or 0
            vocab = cfg.get("vocab_size") or 0
            inter = cfg.get("intermediate_size") or 4 * h
            params_est = int(layers * (4 * h * h + 3 * h * inter) + 2 * vocab * h)
        except Exception as exc:  # noqa: BLE001
            add("model_config_readable", False, f"config.json unreadable: {exc}")
    wbytes = sum((model_dir / f).stat().st_size for f in weights) if weights else 0
    estimate = {}
    if params_est:
        bytes_per = 0.55 if spec.get("method") == "qlora" else 2.0
        need = params_est * bytes_per * 1.35 + 2 * 1024 ** 3
        estimate = {"label": "ESTIMATE ONLY — actual use depends on sequence length and batch size",
                    "parameters_estimated": params_est, "memory_needed_bytes_estimated": int(need)}
        if cuda:
            best = max(g["vram_free"] for g in hw["gpus"])
            add("gpu_memory", best >= need, f"free VRAM {best/2**30:.1f} GiB vs estimated need {need/2**30:.1f} GiB")
        else:
            avail = hw.get("MemAvailable", 0)
            add("ram", avail >= need * 2, f"available RAM {avail/2**30:.1f} GiB vs estimated CPU need "
                                          f"{need*2/2**30:.1f} GiB")
    workdir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(workdir).free
    need_disk = max(2 * 1024 ** 3, int(wbytes * 0.3) + 1024 ** 3)
    add("disk_space", free >= need_disk, f"{free/2**30:.1f} GiB free, need ≈{need_disk/2**30:.1f} GiB")
    add("dataset_examples", examples >= 1, f"{examples} training example(s)")
    ok = all(c["ok"] for c in checks if c["blocking"])
    return {"ok": ok, "checks": checks, "estimate": estimate, "software": sw, "hardware": hw}


# ------------------------------------------------------------------ train
def train(job: Path) -> int:
    status_path = job / "status.json"
    cfg = json.loads((job / "config.json").read_text())
    started = time.time()
    status = {"state": "running", "started_at": started, "pid": os.getpid()}
    write_json(status_path, status)
    try:
        import torch
        from transformers import (AutoModelForCausalLM, AutoTokenizer, Trainer, TrainerCallback,
                                  TrainingArguments, set_seed)
        from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

        spec = dict(RECIPES[cfg["recipe"]])
        for k, v in (cfg.get("params") or {}).items():
            if k in BOUNDS:
                lo, hi = BOUNDS[k]
                spec[k] = type(spec[k])(min(max(v, lo), hi))
        set_seed(int(cfg["seed"]))
        model_dir = cfg["base_model_path"]
        tok = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token or tok.unk_token
        kwargs = {"local_files_only": True}
        cuda = torch.cuda.is_available()
        if spec["method"] == "qlora":
            from transformers import BitsAndBytesConfig
            kwargs["quantization_config"] = BitsAndBytesConfig(
                load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_use_double_quant=True,
                bnb_4bit_compute_dtype=torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16)
            kwargs["device_map"] = {"": 0}
        elif cuda:
            kwargs["torch_dtype"] = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        model = AutoModelForCausalLM.from_pretrained(model_dir, **kwargs)
        if spec["method"] == "qlora":
            model = prepare_model_for_kbit_training(model)
        lcfg = LoraConfig(r=spec["r"], lora_alpha=spec["alpha"], lora_dropout=spec["dropout"],
                          target_modules="all-linear", task_type="CAUSAL_LM")
        model = get_peft_model(model, lcfg)

        def load(name):
            p = job / name
            rows = [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []
            return [format_example(tok, r, spec["max_len"]) for r in rows]
        train_ds, val_ds = load("train.jsonl"), load("validation.jsonl")
        if not train_ds:
            raise RuntimeError("No training examples were provided")

        def collate(batch):
            n = max(len(b["input_ids"]) for b in batch)
            pad = tok.pad_token_id
            return {"input_ids": torch.tensor([b["input_ids"] + [pad] * (n - len(b["input_ids"])) for b in batch]),
                    "attention_mask": torch.tensor([b["attention_mask"] + [0] * (n - len(b["attention_mask"])) for b in batch]),
                    "labels": torch.tensor([b["labels"] + [-100] * (n - len(b["labels"])) for b in batch])}

        metrics_path = job / "metrics.jsonl"

        class Monitor(TrainerCallback):
            cancelled = False

            def on_log(self, args, state, control, logs=None, **kw):
                logs = logs or {}
                if any(k in logs for k in ("loss", "eval_loss")):
                    with metrics_path.open("a") as fh:
                        fh.write(json.dumps({"step": state.global_step, "epoch": logs.get("epoch", state.epoch),
                                             "loss": logs.get("loss"), "eval_loss": logs.get("eval_loss"),
                                             "learning_rate": logs.get("learning_rate"), "time": time.time()}) + "\n")
                write_json(status_path, {**status, "step": state.global_step, "max_steps": state.max_steps,
                                         "heartbeat": time.time()})

            def on_step_end(self, args, state, control, **kw):
                if TEST_STEP_DELAY:                      # automated-test hook only: slows steps, changes nothing else
                    time.sleep(TEST_STEP_DELAY)
                if (job / "CANCEL").exists():
                    Monitor.cancelled = True
                    control.should_save = True
                    control.should_training_stop = True
                return control

        ckpt_dir = job / "checkpoints"
        resume = None
        existing = sorted(ckpt_dir.glob("checkpoint-*"), key=lambda p: int(p.name.split("-")[-1])) if ckpt_dir.exists() else []
        if existing:
            resume = str(existing[-1])
        args = TrainingArguments(
            output_dir=str(ckpt_dir), num_train_epochs=spec["epochs"], learning_rate=spec["lr"],
            per_device_train_batch_size=spec["batch_size"], per_device_eval_batch_size=spec["batch_size"],
            gradient_accumulation_steps=spec["grad_accum"], logging_steps=1, save_strategy="steps",
            save_steps=max(1, int(cfg.get("save_steps", 50))), save_total_limit=2,
            eval_strategy="epoch" if val_ds else "no", report_to=[], seed=int(cfg["seed"]),
            bf16=cuda and torch.cuda.is_bf16_supported() and spec["method"] != "qlora",
            fp16=cuda and not torch.cuda.is_bf16_supported(), use_cpu=not cuda,
            gradient_checkpointing=cuda, remove_unused_columns=False, dataloader_num_workers=0)
        trainer = Trainer(model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds or None,
                          data_collator=collate, callbacks=[Monitor()])
        if resume:
            status["resumed_from"] = resume
        result = trainer.train(resume_from_checkpoint=resume)
        adapter = job / "adapter"
        model.save_pretrained(str(adapter))
        tok.save_pretrained(str(adapter))
        final = {**status, "state": "cancelled" if Monitor.cancelled else "completed", "finished_at": time.time(),
                 "train_runtime": result.metrics.get("train_runtime"), "train_loss": result.metrics.get("train_loss"),
                 "global_step": trainer.state.global_step, "software": software(), "hardware": hardware(),
                 "adapter_files": file_hashes(adapter), "recipe_resolved": spec}
        if val_ds and not Monitor.cancelled:
            final["eval_metrics"] = trainer.evaluate()
        write_json(status_path, final)
        return 0
    except Exception as exc:  # noqa: BLE001
        oom = "out of memory" in str(exc).lower() or type(exc).__name__ == "OutOfMemoryError"
        write_json(status_path, {**status, "state": "failed", "finished_at": time.time(),
                                 "error_code": "out_of_memory" if oom else "error",
                                 "error": ("GPU/CPU ran out of memory. Reduce max_len or batch size, or use QLoRA. "
                                           if oom else "") + f"{type(exc).__name__}: {str(exc)[:800]}",
                                 "trace": traceback.format_exc()[-3000:]})
        return 1


# ------------------------------------------------------------------ evaluate
def generate_all(model, tok, rows, max_new_tokens):
    import torch
    outs = []
    for r in rows:
        prompt = format_example(tok, r, 4096, with_answer=False)
        ids = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
        t0 = time.time()
        with torch.no_grad():
            gen = model.generate(**ids, max_new_tokens=max_new_tokens, do_sample=False,
                                 pad_token_id=tok.pad_token_id or tok.eos_token_id)
        text = tok.decode(gen[0][ids["input_ids"].shape[1]:], skip_special_tokens=True)
        outs.append({"id": r["id"], "output": text.strip(), "latency_ms": int((time.time() - t0) * 1000)})
    return outs


def evaluate(job: Path, heldout: Path, out: Path, max_new_tokens: int) -> int:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel
        cfg = json.loads((job / "config.json").read_text())
        rows = [json.loads(l) for l in heldout.read_text().splitlines() if l.strip()]
        tok = AutoTokenizer.from_pretrained(cfg["base_model_path"], local_files_only=True)
        if tok.pad_token is None:
            tok.pad_token = tok.eos_token or tok.unk_token
        kw = {"local_files_only": True}
        if torch.cuda.is_available():
            kw["torch_dtype"] = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
            kw["device_map"] = {"": 0}
        base = AutoModelForCausalLM.from_pretrained(cfg["base_model_path"], **kw)
        base.eval()
        base_out = generate_all(base, tok, rows, max_new_tokens)
        cand = PeftModel.from_pretrained(base, str(job / "adapter"))
        cand.eval()
        cand_out = generate_all(cand, tok, rows, max_new_tokens)
        write_json(out, {"state": "completed", "base": base_out, "candidate": cand_out,
                         "settings": {"decoding": "greedy", "max_new_tokens": max_new_tokens, "system_prompt": SYSTEM},
                         "software": software(), "hardware": hardware()})
        return 0
    except Exception as exc:  # noqa: BLE001
        write_json(out, {"state": "failed", "error": f"{type(exc).__name__}: {str(exc)[:800]}",
                         "trace": traceback.format_exc()[-3000:]})
        return 1


def merge(job: Path, out: Path) -> int:
    try:
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from peft import PeftModel
        cfg = json.loads((job / "config.json").read_text())
        base = AutoModelForCausalLM.from_pretrained(cfg["base_model_path"], local_files_only=True)
        merged = PeftModel.from_pretrained(base, str(job / "adapter")).merge_and_unload()
        out.mkdir(parents=True, exist_ok=True)
        merged.save_pretrained(str(out), safe_serialization=True)
        AutoTokenizer.from_pretrained(cfg["base_model_path"], local_files_only=True).save_pretrained(str(out))
        write_json(out / "nexagent-merge.json", {"state": "completed", "files": file_hashes(out)})
        return 0
    except Exception as exc:  # noqa: BLE001
        out.mkdir(parents=True, exist_ok=True)
        write_json(out / "nexagent-merge.json", {"state": "failed", "error": f"{type(exc).__name__}: {exc}"[:900]})
        return 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    p = sub.add_parser("preflight"); p.add_argument("--model", required=True); p.add_argument("--workdir", required=True)
    p.add_argument("--recipe", default="lora-sft"); p.add_argument("--examples", type=int, default=0)
    t = sub.add_parser("train"); t.add_argument("--job", required=True)
    e = sub.add_parser("evaluate"); e.add_argument("--job", required=True); e.add_argument("--heldout", required=True)
    e.add_argument("--out", required=True); e.add_argument("--max-new-tokens", type=int, default=300)
    m = sub.add_parser("merge"); m.add_argument("--job", required=True); m.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    if a.mode == "preflight":
        print(json.dumps(preflight(Path(a.model), Path(a.workdir), a.recipe, a.examples)))
        return 0
    if a.mode == "train":
        return train(Path(a.job))
    if a.mode == "evaluate":
        return evaluate(Path(a.job), Path(a.heldout), Path(a.out), a.max_new_tokens)
    return merge(Path(a.job), Path(a.out))


if __name__ == "__main__":
    sys.exit(main())
