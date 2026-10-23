"""Chat locally with the NPC LoRA experiment, or compare its base model."""
import argparse
import json
from contextlib import nullcontext
from pathlib import Path

import torch
from peft import PeftModel
from transformers import AutoModelForCausalLM, AutoTokenizer, TextStreamer


def main():
    root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=root / "outputs/training/character_npc/qwen3-8b-sft-v1")
    parser.add_argument("--speaker", default="你", help="你的角色名，例如 高松燈")
    parser.add_argument("--temperature", type=float, default=0, help="0 为确定性回答；可设为 0.7")
    parser.add_argument("--max-new-tokens", type=int, default=128)
    args = parser.parse_args()
    if not 0 <= args.temperature <= 2 or args.max_new_tokens < 1:
        parser.error("temperature 必须在 0～2 之间，max-new-tokens 必须大于 0")
    if not args.speaker.strip():
        parser.error("speaker 不能为空")
    if not torch.backends.mps.is_available():
        parser.error("需要可用的 Apple Silicon MPS GPU；请使用已安装的训练环境")

    run = args.run.expanduser().resolve()
    report = json.loads((run / "report.json").read_text())
    dataset = root / report["arguments"]["dataset"]
    with (dataset / "train.jsonl").open() as source:
        system = json.loads(next(source))["messages"][0]
    limit = report["arguments"]["max_length"]
    if args.max_new_tokens >= limit:
        parser.error(f"max-new-tokens 必须小于本次实验的上下文上限 {limit}")
    print("加载 Qwen3-8B 和 LoRA，只读取本地文件……", flush=True)
    tokenizer = AutoTokenizer.from_pretrained(run / "adapter", local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        report["model"], dtype=torch.bfloat16, attn_implementation="sdpa", local_files_only=True,
    ).to("mps")
    model = PeftModel.from_pretrained(model, run / "adapter", local_files_only=True).eval()
    sampling = args.temperature > 0
    generation = {
        "max_new_tokens": args.max_new_tokens, "do_sample": sampling,
        "temperature": args.temperature if sampling else 1.0,
        "top_p": 0.9 if sampling else 1.0, "top_k": 20 if sampling else 50,
        "use_cache": True, "pad_token_id": tokenizer.pad_token_id,
    }
    eos_ids = model.generation_config.eos_token_id
    eos_ids = {eos_ids} if isinstance(eos_ids, int) else set(eos_ids or [])
    help_text = "/base 基模 | /lora 微调 | /retry 重答上一句 | /reset 清空 | exit 退出"
    print(f"当前：LoRA；关闭思考；保留最近 4 句前文。\n{help_text}")
    history = []
    last_context = None
    adapted = True
    while True:
        try:
            text = input(f"\n{args.speaker}> ").strip()
            if text in {"exit", "quit", "/quit"}:
                break
            if not text:
                continue
            if text == "/help":
                print(help_text)
                continue
            if text in {"/base", "/lora"}:
                adapted = text == "/lora"
                print(f"当前：{'LoRA' if adapted else '基模'}；输入 /retry 可重答上一句")
                continue
            if text == "/reset":
                history = []
                last_context = None
                print("已清空对话")
                continue
            if text == "/retry":
                if last_context is None:
                    print("还没有可重答的对话")
                    continue
                context = last_context.copy()
            elif text.startswith("/"):
                print(help_text)
                continue
            else:
                context = (history + [f"{args.speaker}：{text}"])[-4:]
            while True:
                messages = [system, {"role": "user", "content": "\n".join(context)}]
                inputs = tokenizer.apply_chat_template(
                    messages, add_generation_prompt=True, enable_thinking=False,
                    return_tensors="pt", return_dict=True,
                )
                if inputs["input_ids"].shape[1] + args.max_new_tokens <= limit or len(context) == 1:
                    break
                context = context[1:]
            if inputs["input_ids"].shape[1] + args.max_new_tokens > limit:
                print("这条输入太长，请缩短后重试；当前对话未改变")
                continue
            inputs = inputs.to("mps")
            print(f"爱音（{'LoRA' if adapted else '基模'}）> ", end="", flush=True)
            adapter_context = nullcontext() if adapted else model.disable_adapter()
            with torch.inference_mode(), adapter_context:
                output = model.generate(
                    **inputs, **generation,
                    streamer=TextStreamer(tokenizer, skip_prompt=True, skip_special_tokens=True),
                )
            tokens = output[0, inputs["input_ids"].shape[1]:]
            reply = tokenizer.decode(tokens, skip_special_tokens=True).strip()
            last_context = context.copy()
            history = (context + [f"千早愛音：{reply}"])[-4:]
            if len(tokens) == args.max_new_tokens and int(tokens[-1]) not in eos_ids:
                print("（达到本次回答长度上限）")
        except (EOFError, KeyboardInterrupt):
            print("\n已退出")
            break


if __name__ == "__main__":
    main()
