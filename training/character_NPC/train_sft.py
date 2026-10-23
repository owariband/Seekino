"""Train and reload a small LoRA adapter on Apple Silicon using PyTorch."""
import argparse
import json
import math
import resource
import time
from pathlib import Path

import torch
from peft import LoraConfig, PeftModel, get_peft_model, get_peft_model_state_dict
from transformers import AutoModelForCausalLM, AutoTokenizer, DataCollatorForSeq2Seq, Trainer, TrainingArguments, set_seed


def encode(rows, tokenizer, maximum_length):
    encoded = []
    accepted = []
    for row in rows:
        prompt = tokenizer.apply_chat_template(row["messages"][:-1], add_generation_prompt=True, enable_thinking=False)
        tokens = tokenizer.apply_chat_template(row["messages"], enable_thinking=False)
        if tokens[:len(prompt)] != prompt:
            raise ValueError("Chat template does not preserve the prompt token prefix")
        if len(tokens) > maximum_length:
            continue
        labels = [-100] * len(prompt) + tokens[len(prompt):]
        if not any(label != -100 for label in labels):
            raise ValueError("Sample has no supervised response")
        encoded.append({"input_ids": tokens, "attention_mask": [1] * len(tokens), "labels": labels})
        accepted.append(row)
    if not encoded:
        raise ValueError("No samples fit the requested context length")
    return encoded, accepted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("dataset", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--max-steps", type=int, default=-1)
    parser.add_argument("--max-length", type=int, default=512)
    parser.add_argument("--epochs", type=float, default=1)
    args = parser.parse_args()
    if not torch.backends.mps.is_available():
        raise RuntimeError("This experiment requires an available PyTorch MPS device")
    args.output.mkdir(parents=True, exist_ok=False)
    set_seed(42)
    tokenizer = AutoTokenizer.from_pretrained(args.model, local_files_only=True)
    raw = {split: [json.loads(line) for line in (args.dataset / f"{split}.jsonl").read_text().splitlines()]
           for split in ("train", "validation")}
    prepared = {split: encode(rows, tokenizer, args.max_length) for split, rows in raw.items()}
    data = {split: pair[0] for split, pair in prepared.items()}
    model = AutoModelForCausalLM.from_pretrained(
        args.model, torch_dtype=torch.bfloat16, attn_implementation="sdpa", local_files_only=True,
    ).to("mps")
    model.config.use_cache = False
    model = get_peft_model(model, LoraConfig(
        task_type="CAUSAL_LM", r=8, lora_alpha=16, target_modules=["q_proj", "v_proj"],
    ))
    model.print_trainable_parameters()
    if any(p.requires_grad for name, p in model.named_parameters() if "lora_" not in name):
        raise RuntimeError("Non-adapter parameters must remain frozen")
    before = {name: value.detach().cpu().clone() for name, value in get_peft_model_state_dict(model).items()}
    trainer = Trainer(
        model=model, train_dataset=data["train"], eval_dataset=data["validation"],
        data_collator=DataCollatorForSeq2Seq(tokenizer, padding=True, label_pad_token_id=-100),
        args=TrainingArguments(
            output_dir=str(args.output / "trainer"), num_train_epochs=args.epochs,
            max_steps=args.max_steps, per_device_train_batch_size=1, per_device_eval_batch_size=1,
            gradient_accumulation_steps=4, learning_rate=1e-4, optim="adamw_torch",
            gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
            save_strategy="no", logging_steps=1, logging_nan_inf_filter=False, report_to="none", seed=42,
            dataloader_pin_memory=False,
        ),
    )
    initial_eval = trainer.evaluate()
    torch.mps.synchronize()
    started = time.monotonic()
    result = trainer.train()
    torch.mps.synchronize()
    elapsed = time.monotonic() - started
    after = {name: value.detach().cpu().clone() for name, value in get_peft_model_state_dict(model).items()}
    if not math.isfinite(result.training_loss) or not all(torch.isfinite(value).all() for value in after.values()):
        raise RuntimeError("Training loss or adapter weights are nonfinite")
    if not any(not torch.equal(before[name], value) for name, value in after.items()):
        raise RuntimeError("Optimizer did not change any adapter weights")
    final_eval = trainer.evaluate()
    model.save_pretrained(args.output / "adapter")
    tokenizer.save_pretrained(args.output / "adapter")
    base = model.unload()
    reloaded = PeftModel.from_pretrained(base, args.output / "adapter").eval()
    restored = get_peft_model_state_dict(reloaded)
    if not all(torch.equal(value, restored[name].detach().cpu()) for name, value in after.items()):
        raise RuntimeError("Saved adapter failed exact reload verification")
    comparisons = []
    for row in prepared["validation"][1][:3]:
        inputs = tokenizer.apply_chat_template(row["messages"][:-1], add_generation_prompt=True,
                                              enable_thinking=False, return_tensors="pt").to("mps")
        def reply():
            with torch.inference_mode():
                output = reloaded.generate(inputs, max_new_tokens=64, do_sample=False)
            return tokenizer.decode(output[0, inputs.shape[1]:], skip_special_tokens=True)
        adapted = reply()
        with reloaded.disable_adapter():
            baseline = reply()
        comparisons.append({"prompt": row["messages"][:-1], "reference": row["messages"][-1]["content"],
                            "timeline_index": row["timeline_index"], "episode": row["episode"],
                            "base": baseline, "adapter": adapted, "source": row["source"]})
    report = {"model": str(args.model.resolve()), "arguments": vars(args), "train_seconds": elapsed,
              "counts": {split: len(rows) for split, rows in data.items()},
              "overlength_skipped": {split: len(raw[split]) - len(data[split]) for split in data},
              "initial_eval": initial_eval, "final_eval": final_eval, "train_metrics": result.metrics,
              "adapter_changed_and_reloaded": True, "peak_process_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
              "mps_driver_allocated_bytes": torch.mps.driver_allocated_memory(), "comparisons": comparisons}
    (args.output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
