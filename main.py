import argparse
import os 
import numpy as np
import torch
from transformers import AutoTokenizer, AutoModelForCausalLM
from importlib.metadata import version

from lib.prune import prune_wanda, prune_magnitude, prune_sparsegpt, check_sparsity, find_layers, prune_wandinter
from lib.eval import eval_ppl, eval_zero_shot

print('torch', version('torch'))
print('transformers', version('transformers'))
print('accelerate', version('accelerate'))
print('# of gpus: ', torch.cuda.device_count())

def get_llm(model, cache_dir="llm_weights"):
    model = AutoModelForCausalLM.from_pretrained(
        model, 
        trust_remote_code=True,
        torch_dtype=torch.float16, 
        low_cpu_mem_usage=True, 
        device_map="auto"
    )
    print(model.hf_device_map)

    model.seqlen = 2048
    return model

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', type=str, help='LLaMA model')
    parser.add_argument('--seed', type=int, default=0, help='Seed for sampling the calibration data.')
    parser.add_argument('--nsamples', type=int, default=128, help='Number of calibration samples.')
    parser.add_argument('--sparsity_ratio', type=float, default=0, help='Sparsity level')
    parser.add_argument("--sparsity_type", type=str, choices=["unstructured", "4:8", "2:4"])
    parser.add_argument("--test_data", type=str, choices=["c4", "wikitext2", 'zerotask', 'lm'])
    parser.add_argument("--prune_method", type=str, choices=["magnitude", "wanda", "sparsegpt", "wandinter"])
    parser.add_argument("--cache_dir", default="llm_weights", type=str )
    parser.add_argument('--use_variant', action="store_true", help="whether to use the wanda variant described in the appendix")
    parser.add_argument('--save', type=str, default=None, help='Path to save results.')
    parser.add_argument('--save_model', type=str, default=None, help='Path to save the pruned model.')
    parser.add_argument("--eval_zero_shot", action="store_true")
    args = parser.parse_args()

    # Setting seeds for reproducibility
    np.random.seed(args.seed)
    torch.random.manual_seed(args.seed)

    # Handling n:m sparsity
    prune_n, prune_m = 0, 0
    if args.sparsity_type != "unstructured":
        assert args.sparsity_ratio == 0.5, "sparsity ratio must be 0.5 for structured N:M sparsity"
        prune_n, prune_m = map(int, args.sparsity_type.split(":"))

    model_name = args.model.split("/")[-1]
    print(f"loading llm model {args.model}")
    model = get_llm(args.model, args.cache_dir)
    model.eval()
    tokenizer = AutoTokenizer.from_pretrained(args.model, use_fast=False, padding_side="left",)

    device = torch.device("cuda:0")
    if "34B" in args.model or "70b" in args.model: # for 30b and 65b we use device_map to load onto multiple A6000 GPUs, thus the processing here.
        device = model.hf_device_map["lm_head"]
    print("use device ", device)

    if args.sparsity_ratio != 0:
        print("pruning starts")
        if args.prune_method == "wanda":
            prune_wanda(args, model, tokenizer, device, prune_n=prune_n, prune_m=prune_m)
        elif args.prune_method == "magnitude":
            prune_magnitude(args, model, tokenizer, device, prune_n=prune_n, prune_m=prune_m)
        elif args.prune_method == "sparsegpt":
            prune_sparsegpt(args, model, tokenizer, device, prune_n=prune_n, prune_m=prune_m)
        elif args.prune_method == "wandinter":
            prune_wandinter(args, model, tokenizer, device, prune_n=prune_n, prune_m=prune_m)

    ################################################################
    print("*"*30)
    sparsity_ratio = check_sparsity(model)
    print(f"sparsity sanity check {sparsity_ratio:.4f}")
    print("*"*30)
    ################################################################
    if not os.path.exists(args.save):
        os.makedirs(args.save)
    ppl = eval_ppl(model, tokenizer, "wikitext2" ,device)
    print(f"ppl on wikitext {ppl}")


    save_filepath = os.path.join(args.save, "wikitext_log.txt")
    with open(save_filepath, "w") as f:
        print("actual_sparsity\tppl", file=f, flush=True)
        print(f"{sparsity_ratio:.4f}\t{ppl:.4f}", file=f, flush=True)


    if args.eval_zero_shot:
        accelerate=False
        if "34b" in args.model or "65b" in args.model or "70b" in args.model:
            accelerate=True

        task_list = ["hellaswag","winogrande", "arc_easy","arc_challenge", "piqa"]
        # task_list = ["hendrycksTest-*"]
        num_shot = 0
        results = eval_zero_shot(args.model, model, tokenizer, task_list, num_shot, accelerate)
        print("********************************")
        print("zero_shot evaluation results")
        print(results)
        save_filepath = os.path.join(args.save, f"{args.sparsity_ratio}zerotask.txt")
        with open(save_filepath, "w") as f:
            # print("actual_sparsity\tppl", file=f, flush=True)
            print(results, file=f, flush=True)

    if args.save_model:
        if not os.path.exists(args.save_model):
            os.makedirs(args.save_model)
        model.save_pretrained(args.save_model)
        tokenizer.save_pretrained(args.save_model)

if __name__ == '__main__':
    main()