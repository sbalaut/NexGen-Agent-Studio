#!/usr/bin/env python3
"""TEST FIXTURE ONLY — builds a tiny, randomly initialised Llama-architecture model
and a small BPE tokenizer entirely offline, so the real training runner can be
verified end-to-end on a CPU. The resulting model produces meaningless text; it
exists only to prove that preflight → LoRA training → metrics → adapter →
evaluation → merge work. Never use it for answers.

Usage (inside the training venv):  python make_tiny_test_model.py OUT_DIR
"""
import json
import sys
from pathlib import Path

from tokenizers import Tokenizer, models, pre_tokenizers, trainers, decoders
from transformers import LlamaConfig, LlamaForCausalLM, PreTrainedTokenizerFast

out = Path(sys.argv[1] if len(sys.argv) > 1 else "tiny-test-model")
out.mkdir(parents=True, exist_ok=True)
corpus = (Path(__file__).resolve().parent.parent / "samples").glob("*.md")
text = "\n".join(p.read_text() for p in corpus) * 3
tok = Tokenizer(models.BPE(unk_token="<unk>"))
tok.pre_tokenizer = pre_tokenizers.ByteLevel(add_prefix_space=False)
tok.decoder = decoders.ByteLevel()
tok.train_from_iterator([text], trainers.BpeTrainer(vocab_size=600, special_tokens=["<unk>", "<s>", "</s>", "<pad>"],
                                                    initial_alphabet=pre_tokenizers.ByteLevel.alphabet()))
fast = PreTrainedTokenizerFast(tokenizer_object=tok, unk_token="<unk>", bos_token="<s>", eos_token="</s>",
                               pad_token="<pad>")
fast.save_pretrained(out)
cfg = LlamaConfig(vocab_size=fast.vocab_size, hidden_size=64, intermediate_size=128, num_hidden_layers=2,
                  num_attention_heads=4, num_key_value_heads=4, max_position_embeddings=4096,
                  bos_token_id=fast.bos_token_id, eos_token_id=fast.eos_token_id, pad_token_id=fast.pad_token_id)
LlamaForCausalLM(cfg).save_pretrained(out, safe_serialization=True)
(out / "LICENSE").write_text("Test fixture generated locally; random weights; no third-party model content.\n")
(out / "nexagent-test-fixture.json").write_text(json.dumps({"purpose": "verification only", "random_weights": True}))
print(f"tiny test model written to {out}")
