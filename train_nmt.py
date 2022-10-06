# -*- coding: utf-8 -*-
# Copyright 2021 National Institute of Information and Communication Technology (Raj Dabre)
# 
# Permission is hereby granted, free of charge, to any person
# obtaining a copy of this software and associated
# documentation files (the "Software"), to deal in the
# Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute,
# sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
# The above copyright notice and this permission notice shall
# be included in all copies or substantial portions of the
# Software.
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
# KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE
# WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS
# OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR
# OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

## Basic imports
import os
import sys
import argparse
import time
os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
##

## Huggingface imports
import transformers
from transformers import AutoTokenizer, MBartTokenizer, MBart50Tokenizer, BartTokenizer, AlbertTokenizer, BarthezTokenizer
from transformers import MBartForConditionalGeneration, BartForConditionalGeneration, MBartConfig, BartConfig,  get_linear_schedule_with_warmup
from transformers import AdamW
##

## Pytorch imports
import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
import torch.multiprocessing as mp
import torch.distributed as dist
from torch.optim import Adam
from torch.utils.tensorboard import SummaryWriter
try:
    import bitsandbytes as bnb
except:
    bnb=None
    print("Bits and bytes not installed. Dont use the flag --adam_8bit")

##

## Our imports
from common_utils import *
##

## Other imports
import math
import random
import numpy as np
import sacrebleu
from rouge_score import rouge_scorer
import gc
import functools
from prefetch_generator import BackgroundGenerator
from m2scorer import get_m2score  # for GEC task we'll use M2 score instead of BLEU
import pdb
##

## Seed setting here
torch.manual_seed(621311)
##

def model_create_load_run_save(gpu, args, train_files, dev_files):
    """The main function which does the overall training. Should be split into multiple parts in the future. Currently monolithc intentionally."""
    
    rank = args.nr * args.gpus + gpu ## The rank of the current process out of the total number of processes indicated by world_size.
    print("Launching process:", rank)
    dist.init_process_group(backend='nccl', init_method='env://', world_size=args.world_size, rank=rank)
    
    if args.shard_files and rank == 0: ## First shard the data using process 0 aka the prime process or master process. Other processes will wait.
        shard_files_bi(train_files, args)
        if args.use_dev_for_fisher:
            shard_files_bi(dev_files, args)
    
    if rank == 0:
        with open(args.model_path + ".quitflag", "w") as f:
            f.write("0")
    dist.barrier() ## Stop other processes from proceeding till sharding is done.
    
    if args.use_official_pretrained_tokenizer or args.use_official_pretrained: # If we use an official model then we are using its tokenizer by default.
        if "mbart" in args.pretrained_model or "IndicBART" in args.pretrained_model:
            if "50" in args.pretrained_model:
                tok = MBart50Tokenizer.from_pretrained(args.tokenizer_name_or_path, use_fast=False)
            elif "IndicBART" in args.pretrained_model:
                tok = AlbertTokenizer.from_pretrained(args.tokenizer_name_or_path, do_lower_case=False, use_fast=False, keep_accents=True)
            else:
                tok = MBartTokenizer.from_pretrained(args.tokenizer_name_or_path, use_fast=False)
        else:
            tok = BartTokenizer.from_pretrained(args.tokenizer_name_or_path, use_fast=False)
    else:
        if "albert" in args.tokenizer_name_or_path:
            tok = AlbertTokenizer.from_pretrained(args.tokenizer_name_or_path, do_lower_case=False, use_fast=False, keep_accents=True)
        elif "mbart" in args.tokenizer_name_or_path:
            tok = MBartTokenizer.from_pretrained(args.tokenizer_name_or_path, do_lower_case=False, use_fast=False, keep_accents=True)
        ## Fast tokenizers are not good because their behavior is weird. Accents should be kept or else the segmentation will be messed up on languages with accented characters. No lower case obviously because we want to train on the original case. Set to false if you are ok with the model not dealing with cases.
    scorer = rouge_scorer.RougeScorer(['rouge1', 'rougeL'], use_stemmer=False) ## In case we do summarization.
    tok.save_pretrained(args.model_path+"_deploy") ## Save the tokenizer for future use.
    print("Tokenizer is:", tok)

    if args.supported_languages is not None:
        args.supported_languages = args.supported_languages.split(",")
        with open(args.model_path+"_deploy/supported_languages.txt", "w") as f:
            for supported_pair in args.supported_languages:
                f.write(supported_pair.replace("-", " ")+"\n")
    
    print(f"Running DDP checkpoint example on rank {rank}.")
    
    if args.fp16: ## Although the code supports FP16/AMP training, it tends to be unstable in distributed setups so use this carefully.
        print("We will do fp16 training")
        scaler = torch.cuda.amp.GradScaler(args.init_scale) ## Gradient scaler which will be used with torch's automatic mixed precision
        # Get scaler info
        scaler_info = scaler.state_dict()
        # Print scaler info neatly
        print("AMP scaler info:")
        for key, value in scaler_info.items():
            print(f"{key}: {value}")
        # Store current scale value
        scale_value = scaler.get_scale()
    else:
        print("We will do fp32 training")
    
    if args.encoder_tying_config is not None:
        print("We will use recurrently stacked layers for the encoder with configuration:", args.encoder_tying_config)
    if args.decoder_tying_config is not None:
        print("We will use recurrently stacked layers for the decoder with configuration:", args.decoder_tying_config)
    
    if args.unidirectional_encoder:
        print("Using unidirectional encoder.")
    
    if rank == 0:
        writer = SummaryWriter(args.model_path+".tflogs")
    
    if args.use_official_pretrained:
        if "mbart" in args.pretrained_model or "IndicBART" in args.pretrained_model:
            config = MBartConfig.from_pretrained(args.pretrained_model)
            config.init_std = args.init_std # We should set the init_std to be different when using adaptors or newer params.
            config.dropout = args.dropout ## We should set dropouts manually
            config.attention_dropout = args.attention_dropout ## We should set dropouts manually
            config.activation_dropout = args.activation_dropout ## We should set dropouts manually
            config.encoder_layerdrop = args.layerdrop ## We should set dropouts manually
            config.decoder_layerdrop = args.layerdrop ## We should set dropouts manually
            config.prompt_tuning = args.prompt_tuning ## We should set prompt_tuning_info manually
            config.prompt_projection_hidden_size=args.prompt_projection_hidden_size
            config.prompt_init_std=args.prompt_init_std ## We should set prompt_init_std manually
            config.layernorm_prompt_projection=args.layernorm_prompt_projection ## We should set layernorm_prompt_projection manually
            config.no_projection_prompt=args.no_projection_prompt ## We should set no_projection_prompt manually
            config.use_tanh_activation_prompt=args.use_tanh_activation_prompt ## We should set use_tanh_activation_prompt manually
            config.residual_connection_prompt=args.residual_connection_prompt ## We should set residual_connection_prompt manually
            config.num_prompts = args.num_prompts ## We should set num_prompts manually
            config.prompt_dropout = args.prompt_dropout ## We should set prompt_dropout manually
            config.recurrent_projections = args.recurrent_projections ## We should set recurrent_projections manually
            config.adaptor_tuning = args.adaptor_tuning ## We should set adaptor_tuning_info manually
            config.deep_adaptor_tuning = args.deep_adaptor_tuning ## We should set deep_adaptor_tuning_info manually
            config.deep_adaptor_tuning_ffn_only = args.deep_adaptor_tuning_ffn_only ## We should set deep_adaptor_tuning_info manually
            config.adaptor_dropout = args.adaptor_dropout ## We should set adaptor_dropout manually
            config.parallel_adaptors = args.parallel_adaptors ## We should set parallel_adaptors_info manually
            config.layernorm_adaptor_input = args.layernorm_adaptor_input ## We should set layernorm_adaptor_input_info manually
            config.adaptor_scaling_factor = args.adaptor_scaling_factor ## We should set adaptor_scaling_factor_info manually
            config.residual_connection_adaptor = args.residual_connection_adaptor ## We should set residual_connection_adaptor_info manually
            config.encoder_adaptor_tying_config = args.encoder_adaptor_tying_config ## We should set encoder_tying_config manually
            config.decoder_adaptor_tying_config = args.decoder_adaptor_tying_config ## We should set decoder_tying_config manually
            config.adaptor_hidden_size = args.adaptor_hidden_size ## We should set adaptor_hidden_size manually
            config.moe_adaptors=args.moe_adaptors ## We should set moe_adaptors_info manually
            config.num_moe_adaptor_experts=args.num_moe_adaptor_experts ## We should set num_moe_adaptor_experts_info manually
            config.hypercomplex = args.hypercomplex ## We should set hypercomplex manually
            config.hypercomplex_n = args.hypercomplex_n ## We should set hypercomplex_n manually
            config.ia3_adaptors = args.ia3_adaptors ## We should set ia3_adaptors info manually
            config.softmax_bias_tuning = args.softmax_bias_tuning ## We should set softmax_bias_tuning_info manually
            config.gradient_checkpointing = args.gradient_checkpointing ## We should set gradient_checkpointing_info manually
            model = MBartForConditionalGeneration.from_pretrained(args.pretrained_model, config=config) ## We may use FBs official model and fine-tune it for our purposes.
            config.architectures = ["MBartForConditionalGeneration"]
            config.save_pretrained(args.model_path+"_deploy") ## Save the config as a json file to ensure easy loading during future fine tuning of the model.
        elif "bart" in args.pretrained_model:
            config = BartConfig.from_pretrained(args.pretrained_model)
            config.init_std = args.init_std # We should set the init_std to be different when using adaptors or newer params.
            config.dropout = args.dropout ## We should set dropouts manually
            config.attention_dropout = args.attention_dropout ## We should set dropouts manually
            config.activation_dropout = args.activation_dropout ## We should set dropouts manually
            config.encoder_layerdrop = args.layerdrop ## We should set dropouts manually
            config.decoder_layerdrop = args.layerdrop ## We should set dropouts manually
            config.gradient_checkpointing = args.gradient_checkpointing ## We should set gradient_checkpointing_info manually
            model = BartForConditionalGeneration.from_pretrained(args.pretrained_model, config=config, force_bos_token_to_be_generated=True) ## We may use FBs official model and fine-tune it for our purposes.
            config.architectures = ["BartForConditionalGeneration"]
            config.save_pretrained(args.model_path+"_deploy") ## Save the config as a json file to ensure easy loading during future fine tuning of the model.
    else: # We are going to manually specify a config for our locally trained model.
        config = MBartConfig(vocab_size=len(tok), init_std=args.init_std, encoder_layers=args.encoder_layers, decoder_layers=args.decoder_layers, dropout=args.dropout, attention_dropout=args.attention_dropout, activation_dropout=args.activation_dropout, encoder_attention_heads=args.encoder_attention_heads, decoder_attention_heads=args.decoder_attention_heads, encoder_ffn_dim=args.encoder_ffn_dim, decoder_ffn_dim=args.decoder_ffn_dim, d_model=args.d_model, embed_low_rank_dim=args.embed_low_rank_dim, no_embed_norm=args.no_embed_norm, scale_embedding=args.scale_embedding, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"], add_special_tokens=False).input_ids[0][0], bos_token_id=tok(["<s>"], add_special_tokens=False).input_ids[0][0], encoder_tying_config=args.encoder_tying_config, decoder_tying_config=args.decoder_tying_config, gradient_checkpointing=args.gradient_checkpointing, multilayer_softmaxing=args.multilayer_softmaxing, wait_k=args.wait_k, additional_source_wait_k=args.additional_source_wait_k, unidirectional_encoder=args.unidirectional_encoder, multi_source=args.multi_source, multi_source_method=args.multi_source_method, mid_fusion_layers=args.mid_fusion_layers, bottleneck_mid_fusion_tokens=args.bottleneck_mid_fusion_tokens, softmax_temperature=args.softmax_temperature, temperature_calibration=args.temperature_calibration, encoder_layerdrop=args.layerdrop, decoder_layerdrop=args.layerdrop, no_scale_attention_embedding=args.no_scale_attention_embedding, positional_encodings=args.positional_encodings, num_domains_for_domain_classifier=args.num_domains_for_domain_classifier, gradient_reversal_for_domain_classifier=args.gradient_reversal_for_domain_classifier, activation_function=args.activation_function, no_positional_encoding_encoder=args.no_positional_encoding_encoder, no_positional_encoding_decoder=args.no_positional_encoding_decoder, use_moe=args.use_moe, num_experts=args.num_experts, expert_ffn_size=args.expert_ffn_size, prompt_tuning=args.prompt_tuning, prompt_dropout=args.prompt_dropout, prompt_projection_hidden_size=args.prompt_projection_hidden_size, prompt_init_std=args.prompt_init_std, layernorm_prompt_projection=args.layernorm_prompt_projection, no_projection_prompt=args.no_projection_prompt, use_tanh_activation_prompt=args.use_tanh_activation_prompt, residual_connection_prompt=args.residual_connection_prompt, num_prompts=args.num_prompts, recurrent_projections=args.recurrent_projections, adaptor_tuning=args.adaptor_tuning, deep_adaptor_tuning=args.deep_adaptor_tuning, deep_adaptor_tuning_ffn_only=args.deep_adaptor_tuning_ffn_only, adaptor_dropout=args.adaptor_dropout, parallel_adaptors = args.parallel_adaptors, layernorm_adaptor_input = args.layernorm_adaptor_input, adaptor_scaling_factor = args.adaptor_scaling_factor, residual_connection_adaptor = args.residual_connection_adaptor, encoder_adaptor_tying_config=args.encoder_adaptor_tying_config, decoder_adaptor_tying_config=args.decoder_adaptor_tying_config, adaptor_hidden_size=args.adaptor_hidden_size, moe_adaptors=args.moe_adaptors, num_moe_adaptor_experts=args.num_moe_adaptor_experts, hypercomplex=args.hypercomplex, hypercomplex_n=args.hypercomplex_n, ia3_adaptors=args.ia3_adaptors, softmax_bias_tuning=args.softmax_bias_tuning, tokenizer_class="AlbertTokenizer" if "albert" in args.tokenizer_name_or_path else "MBartTokenizer") ## Configuration. TODO: Save this configuration somehow.
        config.architectures = ["MBartForConditionalGeneration"]
        config.save_pretrained(args.model_path+"_deploy") ## Save the config as a json file to ensure easy loading during future fine tuning of the model.
        model = MBartForConditionalGeneration(config)
    model.train()
    
    if args.distillation: ## When distilling we need a parent model. The creation of the model is in the same way as the child. This model is immediately loaded with some pretrained params and then loaded into the GPU.
        print("We will do distillation from a parent model.")
        if args.use_official_parent_pretrained:
            if "mbart" in args.parent_pretrained_model or "IndicBART" in args.pretrained_model:
                parent_config = MBartConfig.from_pretrained(args.parent_pretrained_model)
                parent_config.dropout = args.parent_dropout ## We should set dropouts manually
                parent_config.attention_dropout = args.parent_attention_dropout ## We should set dropouts manually
                parent_config.activation_dropout = args.parent_activation_dropout ## We should set dropouts manually
                parent_config.encoder_layerdrop = args.layerdrop ## We should set dropouts manually
                parent_config.decoder_layerdrop = args.layerdrop ## We should set dropouts manually
                parent_model = MBartForConditionalGeneration.from_pretrained(args.parent_pretrained_model, config=parent_config) ## We may use FBs official model and fine-tune it for our purposes.
            elif "bart" in args.parent_pretrained_model:
                parent_config = BartConfig.from_pretrained(args.parent_pretrained_model)
                parent_config.dropout = args.parent_dropout ## We should set dropouts manually
                parent_config.attention_dropout = args.parent_attention_dropout ## We should set dropouts manually
                parent_config.activation_dropout = args.parent_activation_dropout ## We should set dropouts manually
                parent_config.encoder_layerdrop = args.layerdrop ## We should set dropouts manually
                parent_config.decoder_layerdrop = args.layerdrop ## We should set dropouts manually
                parent_model = BartForConditionalGeneration.from_pretrained(args.parent_pretrained_model, config=parent_config, force_bos_token_to_be_generated=True) ## We may use FBs official model and fine-tune it for our purposes.
        else: ## Its a locally pre-trained parent model.
            parent_config = MBartConfig(vocab_size=len(tok), encoder_layers=args.parent_encoder_layers, decoder_layers=args.parent_decoder_layers, dropout=args.parent_dropout, attention_dropout=args.parent_attention_dropout, activation_dropout=args.parent_activation_dropout, encoder_attention_heads=args.parent_encoder_attention_heads, decoder_attention_heads=args.parent_decoder_attention_heads, encoder_ffn_dim=args.parent_encoder_ffn_dim, decoder_ffn_dim=args.parent_decoder_ffn_dim, d_model=args.parent_d_model, no_embed_norm=args.no_embed_norm, scale_embedding=args.scale_embedding, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"], add_special_tokens=False).input_ids[0][0], bos_token_id=tok(["<s>"], add_special_tokens=False).input_ids[0][0], encoder_tying_config=args.encoder_tying_config, decoder_tying_config=args.decoder_tying_config, wait_k=args.wait_k, additional_source_wait_k=args.additional_source_wait_k, unidirectional_encoder=args.unidirectional_encoder, multi_source=args.multi_source, multi_source_method=args.multi_source_method, mid_fusion_layers=args.mid_fusion_layers, bottleneck_mid_fusion_tokens=args.bottleneck_mid_fusion_tokens, softmax_temperature=args.softmax_temperature, temperature_calibration=args.temperature_calibration, encoder_layerdrop=args.layerdrop, decoder_layerdrop=args.layerdrop, no_scale_attention_embedding=args.no_scale_attention_embedding, positional_encodings=args.positional_encodings, activation_function=args.activation_function, no_positional_encoding_encoder=args.no_positional_encoding_encoder, no_positional_encoding_decoder=args.no_positional_encoding_decoder, use_moe=args.use_moe, num_experts=args.num_experts, expert_ffn_size=args.expert_ffn_size)
            parent_model = MBartForConditionalGeneration(config)
        parent_model.cuda(gpu)
        parent_model.train() ## We do this to enable dropout but we wont have an optimizer for this so we wont train this model. For now. Future implementations should ask if we want to do co-distill or not. By co-distillation I mean, the parent will learn together with the child.
        parent_model = DistributedDataParallel(parent_model, device_ids=[gpu], output_device=gpu)
        print("Loading a parent model from which distillation will be done.")
        dist.barrier()
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % gpu}
        if not args.use_official_parent_pretrained:
            parent_checkpoint_dict = torch.load(args.parent_pretrained_model, map_location=map_location)
            if type(parent_checkpoint_dict) == dict:
                parent_model.load_state_dict(parent_checkpoint_dict['model']) # We never do any remapping of the parent. We always reuse it as it is.
            else:
                parent_model.module.load_state_dict(parent_checkpoint_dict) # We never do any remapping of the parent. We always reuse it as it is.
            del parent_checkpoint_dict
            
        parent_model.train()

    torch.cuda.set_device(gpu) ## Set the device to the current GPU. This is different from the rank so keep this in mind.
    torch.cuda.empty_cache()

    if args.freeze_embeddings: ## If we wish to freeze the model embeddings. This may be useful when fine-tuning a pretrained model.
        print("Freezing embeddings")
        freeze_embeds(model)
    if args.freeze_encoder: ## If we wish to freeze the encoder itself. This may be useful when fine-tuning a pretrained model. This freezes embeddings too so be careful.
        print("Freezing encoder")
        freeze_params(model.get_encoder())
        assert_all_frozen(model.get_encoder())

    freeze_params(model, args.freeze_exception_list)

    ### NOTE: Please freeze params before wrapping the model in DDP. Mandem almost had a stoke trying to figure this out.

    model.cuda(gpu) ## Move the model to the GPU.
    print("Memory consumed after moving model to GPU", round(torch.cuda.memory_allocated(gpu)/(1024**3), 2), "GB")
    model = DistributedDataParallel(model, device_ids=[gpu], output_device=gpu) ## This wrapper around the model will enable distributed training.
    print("Memory consumed after wrapping with DDP", round(torch.cuda.memory_allocated(gpu)/(1024**3), 2), "GB")
    no_decay = ["bias", "LayerNorm.weight"]
    optimizer_grouped_parameters = [
        {
            "params": [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay) and p.requires_grad],
            "weight_decay": args.weight_decay,
        },
        {
            "params": [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay) and p.requires_grad],
            "weight_decay": 0.0,
        },
    ] ## We suppose that weight decay will be used except for biases and layer norm weights.

    print("Optimizing", [n for n, p in model.named_parameters() if p.requires_grad])
    if args.gradient_checkpointing:
        print("Using gradient checkpointing")
    num_params_to_optimize = sum(p.numel() for p in model.parameters() if p.requires_grad)
    num_model_params = sum(p.numel() for p in model.parameters())
    print("Number of model parameters:", num_model_params)
    print("Total number of params to be optimized are: ", num_params_to_optimize)

    print("Percentage of parameters to be optimized: ", 100*num_params_to_optimize/num_model_params)

    if args.prompt_tuning:
        print("Although the percentage of parameters to be optimized is high, during training the number of actual params during decoding are way way lower.")

    if args.adam_8bit:
        print("Using an 8-bit AdamW optimizer.")
        optimizer = bnb.optim.AdamW8bit(optimizer_grouped_parameters, lr=args.lr, eps=args.adam_eps, betas=(0.9, 0.995)) # Our glorious 8 bit optimizer. All hail our lord and savior Tim Dettmers.
    else:
        print("Using an 32-bit AdamW optimizer.")
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.lr, eps=args.adam_eps) ## Our glorious optimizer.
    
    model.train()
    
    
    scheduler = get_linear_schedule_with_warmup(optimizer, args.warmup_steps, args.num_batches) ## A warmup and decay scheduler. We use the linear scheduler for now. TODO: Enable other schedulers with a flag.
    
    while scheduler.get_lr()[0] < 1e-7: ## We want to keep a minimum learning rate else for the initial batch or initial few batches barely anything will be learned which is a waste of computation. This minimum value is kept to 1e-7 by default in accordance with previous literature, other implementations and the Paris peace accords.
        scheduler.step()
    print("Initial LR is:", scheduler.get_lr()[0])
    
    if args.pretrained_model != "" and (not args.use_official_pretrained or args.locally_fine_tuned_model_path is not None): ## Here we load a pretrained NMT model or a previous checkpoint in case training crashed. Note the args.locally_fine_tuned_model_path. This is in case we were tuning an official mbart or indicbart or bart model but want to further tine tune it or it crashed and we want to resume training it.
        print("Loading from checkpoint. Strict loading by default but if there are missing or non matching keys or if we use prompt or adaptor tuning, they will be ignored when layer remapping or component selection is done. In case of prompt and adaptor tuning, new params are added to the model and hence strict matching of keys is not possible.")
        dist.barrier()
        # configure map_location properly
        map_location = {'cuda:%d' % 0: 'cuda:%d' % gpu}
        if args.locally_fine_tuned_model_path is not None: ## Now that the pretrained_model argument was used to instantiate the model, it can be replaced with the local model path. Remember to specify pure model or the model with the optimizer and scheduler states depending on your requirement by relying on the flag --no_reload_optimizer_ctr_and_scheduler.
            args.pretrained_model = args.locally_fine_tuned_model_path
        checkpoint_dict = torch.load(args.pretrained_model, map_location=map_location)
        if type(checkpoint_dict) == dict:
            model.load_state_dict(remap_embeddings_eliminate_components_and_eliminate_mismatches(model.state_dict(), remap_layers(checkpoint_dict['model'], 4, args), args), strict=True if (args.remap_encoder == "" and args.remap_decoder == "" and not args.eliminate_encoder_before_initialization and not args.eliminate_decoder_before_initialization and not args.eliminate_embeddings_before_initialization and not args.prompt_tuning and not args.adaptor_tuning and not args.deep_adaptor_tuning and not args.deep_adaptor_tuning_ffn_only and not args.ia3_adaptors and not args.softmax_bias_tuning) else False)
            if args.prompt_tuning and args.initialize_prompts_with_random_embeddings:
                model.module.initialize_prompt_params_with_random_embeddings()
            if not args.no_reload_optimizer_ctr_and_scheduler and args.remap_encoder is '' and args.remap_decoder is '' and not args.eliminate_encoder_before_initialization and not args.eliminate_decoder_before_initialization and not args.eliminate_embeddings_before_initialization: ## Do not load optimizers, ctr and schedulers when remapping or resuming training.
                if 'optimizer' in checkpoint_dict:
                    print("Reloading optimizer")
                    optimizer.load_state_dict(checkpoint_dict['optimizer']) ## Dubious
                if 'scheduler' in checkpoint_dict:
                    print("Reloading scheduler")
                    scheduler.load_state_dict(checkpoint_dict['scheduler']) ## Dubious
                if 'ctr' in checkpoint_dict:
                    print("Reloading ctr. This means we resume training.")
                    ctr = checkpoint_dict['ctr']
            else:
                ctr = 0
        else:
            model.module.load_state_dict(remap_embeddings_eliminate_components_and_eliminate_mismatches(model.state_dict(), remap_layers(checkpoint_dict, 3, args), args), strict=True if (args.remap_encoder == "" and args.remap_decoder == "" and not args.eliminate_encoder_before_initialization and not args.eliminate_decoder_before_initialization and not args.eliminate_embeddings_before_initialization and not args.prompt_tuning and not args.adaptor_tuning and not args.deep_adaptor_tuning and not args.deep_adaptor_tuning_ffn_only and not args.ia3_adaptors and not args.softmax_bias_tuning) else False)
            if args.prompt_tuning and args.initialize_prompts_with_random_embeddings:
                model.module.initialize_prompt_params_with_random_embeddings()
            ctr = 0
        del checkpoint_dict
    else:
        if args.use_official_pretrained:
            print("Training from official pretrained model")
            if args.prompt_tuning and args.initialize_prompts_with_random_embeddings:
                model.module.initialize_prompt_params_with_random_embeddings()
        else:
            print("Training from scratch")
        CHECKPOINT_PATH = args.model_path
        if rank == 0:
            checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': 0}
            torch.save(checkpoint_dict, CHECKPOINT_PATH) ## Save a model by default every eval_every steps. This model will be saved with the same file name each time.
            torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model")
        dist.barrier()
        map_location = {'cuda:%d' % 0: 'cuda:%d' % gpu}
        checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
        model.load_state_dict(checkpoint_dict['model'])
        optimizer.load_state_dict(checkpoint_dict['optimizer'])
        scheduler.load_state_dict(checkpoint_dict['scheduler'])
        ctr = checkpoint_dict['ctr']
        del checkpoint_dict
        torch.cuda.empty_cache()
        
    model.train()
        
    print("Using label smoothing of", args.label_smoothing)
    print("Using gradient clipping norm of", args.max_gradient_clip_value)
    print("Using softmax temperature of", args.softmax_temperature)
    if args.max_ent_weight != -1:
        print("Doing entropy maximization during loss computation.")
    if args.multistep_optimizer_steps > 1:
        print("Using a multistep optimizer where gradients will be accumulated over", args.multistep_optimizer_steps, "batches.")

    if args.ewc_importance != 0: ## Set up elastic weight consolidation
        print("Using Elastic Weight Consolidation with importance", args.ewc_importance)
        print("Number of training batches to compute Fisher coefficients:", args.ewc_samples)
        num_batches_tmp = args.num_batches
        args.num_batches = args.ewc_samples
        print("Learning Fisher coefficients.")
        if args.use_dev_for_fisher:
            print("Using dev set for computing Fisher coefficients.")
            files = dev_files
        else:
            print("Using train set for computing Fisher coefficients.")
            files = train_files
        if args.use_denoising_prediction_for_fisher:
            print("Using denoising objective for computing Fisher coefficients.")
            print("We will select sentences for each language.") ## Be careful here as languages with the same ids will end up with only one set of sentences among all. Change this to your liking by merging corpora.
            files_tmp = []
            for lang, file_content in files:
                slang, tlang = lang.split("-")
                files_tmp.append([slang+"-"+slang, [file_content[0], file_content[0]]])
                files_tmp.append([tlang+"-"+tlang, [file_content[1], file_content[1]]])
            files = files_tmp
        else:
            print("Using regular seq2seq objective for computing Fisher coefficients.")
        datagenerator = generate_batches_bilingual(tok, args, files, rank)
        ewc_loss = EWC(model, datagenerator, gpu, args.label_smoothing, ignore_index=tok.pad_token_id)
        args.num_batches = num_batches_tmp
        print("Fisher coefficients learned.")
    
    num_batches_this_optimizer_step = 0
    losses = 0
    global_sbleu_history = [] ## To save the global evaluation metric history.
    max_global_sbleu = 0 ## Maximum global evaluation metric score.
    max_global_sbleu_step = 0 ## Step at which we achieved the maximum global evaluation metric score.
    individual_sbleu_history = [[dev_pair, []] for dev_pair in dev_files] ## For multilingual NMT settings we suppose that we will keep a track of the histories for individual language pairs being evaluated and this dictionary keeps track of the history.
    max_individual_sbleu = [[dev_pair, 0] for dev_pair in dev_files] ## The maximum score per pair.
    max_individual_sbleu_step = [[dev_pair, 0] for dev_pair in dev_files] ## The step at which maximum score was achieved per pair.
    curr_eval_step = 0
    annealing_attempt = 0 ## We use this to limit the number of times annealing will take place. When we anneal the LR is divided by a factor. How this is achieved will be explained below.
    inps = [[dev_pair, [inpline.strip() for inpline in open(dev_pair_info[0])][:args.max_eval_batches*args.dev_batch_size]] for dev_pair, dev_pair_info in dev_files] ## Get all inputs for each pair. Select up to args.max_eval_batches*args.dev_batch_size examples.
    if args.is_summarization: ## Slight data structure difference for summarization vs translation when computing the evaluation metric. For summarization the metric is Rouge.
        refs = [[dev_pair, [[refline.strip() for refline in open(dev_pair_info[1])][:args.max_eval_batches*args.dev_batch_size]]] for dev_pair, dev_pair_info in dev_files] ## Get all references for each input. Select up to args.max_eval_batches*args.dev_batch_size examples.
    else:
        refs = [[dev_pair, [[refline.strip() for refline in open(dev_pair_info[1])][:args.max_eval_batches*args.dev_batch_size]]] for dev_pair, dev_pair_info in dev_files] ## Get all references for each input. Select up to args.max_eval_batches*args.dev_batch_size examples.
    if args.use_m2:
        ref_srcs = [[dev_pair, [[refline.strip() for refline in open(dev_pair_info[0])][:args.max_eval_batches*args.dev_batch_size]]] for dev_pair, dev_pair_info in dev_files] ## Get all references for each input. Select up to args.max_eval_batches*args.dev_batch_size examples.
    
    start = time.time()
    
    for input_ids, input_masks, decoder_input_ids, labels in generate_batches_bilingual(tok, args, train_files, rank): #Batches are generated from here. The argument (0.30, 0.40) is a range which indicates the percentage of the source sentence to be masked in case we want masking during training just like we did during BART pretraining. The argument 3.5 is the lambda to the poisson length sampler which indicates the average length of a word sequence that will be masked.
        if ctr % args.eval_every == 0 and num_batches_this_optimizer_step == 0: ## We have to evaluate our model every eval_every steps.
            CHECKPOINT_PATH = args.model_path
            if rank == 0: ## Evaluation will be done only on the prime/master process which is at rank 0. Other processes will sleep.
                if not args.no_eval: ## If we dont care about early stopping and only on training for a bazillion batches then you can save time by skipping evaluation.
                    print("Running eval on dev set(s)")
                    if args.mixed_wait_k:
                        model.module.config.wait_k = args.wait_k
                    hyp = [[dev_pair, []] for dev_pair, dev_pair_info in dev_files]
                    sbleus = []
                    model.eval() ## We go to eval mode so that there will be no dropout.
                    checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr} ## This training state will be saved.
                    for dev_idx, [dev_pair, dev_pair_info] in enumerate(dev_files): ## For each evaluation pair we will decode and compute scores.
                        slangtlang =dev_pair.strip().split("-")
                        if args.multi_source: ## In case we do multisource NMT
                            slang=slangtlang[0]+"-"+slangtlang[1] ## This will be split in the generate_batches_eval function as we expect a triplet. 
                            tlang=slangtlang[2]
                        else:
                            slang=slangtlang[0]
                            tlang=slangtlang[1]
                        eval_batch_counter = 0
                        for dev_input_ids, dev_input_masks in generate_batches_eval_bilingual(tok, args, inps[dev_idx][1], slang):
                            if args.multi_source:
                                dev_input_ids_parent = dev_input_ids[1]
                                dev_input_ids = dev_input_ids[0]
                                dev_input_masks_parent = dev_input_masks[1]
                                dev_input_masks = dev_input_masks[0]
                                dev_input_ids_parent = dev_input_ids_parent.to(gpu) ## Move to GPU.
                                dev_input_masks_parent = dev_input_masks_parent.to(gpu) ## Move to GPU.
                                
                            if args.prompt_tuning:
                                dev_input_shape = dev_input_masks.size()
                                encoder_pad = torch.ones(dev_input_shape[0], args.num_prompts).clone().detach()
                                dev_input_masks = torch.cat([encoder_pad, dev_input_masks], dim=1)
                            start = time.time()
                            dev_input_ids = dev_input_ids.to(gpu) ## Move to GPU.
                            dev_input_masks = dev_input_masks.to(gpu) ## Move to GPU.
                            if args.is_summarization: ## Things can be slow so best show progress
                                print("Decoding batch from a pool of", len(inps[dev_idx][1]), "examples")
                            with torch.no_grad(): ## torch.no_grad is apparently known to prevent the code from allocating memory for gradient computation in addition to making things faster. I have not verified this but have kept it as a safety measure to ensure that my model is not being directly tuned on the development set.
                                translations = model.module.generate(dev_input_ids, use_cache=True, num_beams=1, max_length=int((len(dev_input_ids[0])*args.max_decode_length_multiplier) if args.max_decode_length_multiplier > 0 else -args.max_decode_length_multiplier), min_length=int((len(dev_input_ids[0])*args.min_decode_length_multiplier) if args.min_decode_length_multiplier > 0 else -args.min_decode_length_multiplier), early_stopping=True, attention_mask=dev_input_masks, pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"], add_special_tokens=False).input_ids[0][0], decoder_start_token_id=tok([tlang if args.use_official_pretrained else "<2"+tlang+">"], add_special_tokens=False).input_ids[0][0], bos_token_id=tok(["<s>"], add_special_tokens=False).input_ids[0][0], length_penalty=args.length_penalty, repetition_penalty=args.repetition_penalty, encoder_no_repeat_ngram_size=args.encoder_no_repeat_ngram_size, no_repeat_ngram_size=args.no_repeat_ngram_size, additional_input_ids=dev_input_ids_parent if args.multi_source else None, additional_input_ids_mask=dev_input_masks_parent if args.multi_source else None) ## We translate the batch. 
                            del dev_input_ids ## Delete to avoid retention.
                            del dev_input_masks ## Delete to avoid retention.
                            translations = translations.to('cpu') ## Delete to avoid retention.
                            if args.multi_source:
                                del dev_input_ids_parent ## Delete to avoid retention.
                                del dev_input_masks_parent ## Delete to avoid retention.
                            for translation in translations:
                                translation  = tok.decode(translation, skip_special_tokens=args.no_skip_special_tokens, clean_up_tokenization_spaces=False) ### Get the raw sentences.
                                hyp[dev_idx][1].append(translation)
                            del translations ## Delete to avoid retention.
                        if args.use_rouge: ## Get the evaluation metric score.
                            scores = 0
                            for curr_ref, curr_pred in zip(refs[dev_idx][1][0], hyp[dev_idx][1]):
                                score = scorer.score(curr_ref, curr_pred)
                                scores += score['rougeL'].fmeasure
                            sbleu = scores/len(hyp[dev_idx][1])
                            metric = 'Rouge'
                            scorertool = 'RougeScorer'
                        elif args.use_m2: ## M2score for Grammatical Error Correction task
                            gold_srcs = ref_srcs[dev_idx][1][0]
                            gold_refs = refs[dev_idx][1][0]
                            preds = hyp[dev_idx][1]
                            p, r, f = get_m2score(preds, gold_srcs, gold_refs)
                            sbleu = f
                            metric = 'M2'
                            scorertool = 'M2Scorer'
                        else:
                            sbleu = get_sacrebleu(refs[dev_idx][1], hyp[dev_idx][1])
                            metric = 'BLEU'
                            scorertool = 'SacreBLEU'
                        individual_sbleu_history[dev_idx][1].append([sbleu, ctr]) ## Update the score history for this pair.
                        sbleus.append(sbleu)
                        print(metric, "score using", scorertool, "after", ctr, "iterations is", round(sbleu, 2), "for language pair", dev_pair)
                        writer.add_scalar(dev_pair+" bleu/rouge", sbleu, ctr)
                        if sbleu > max_individual_sbleu[dev_idx][1]: ## Update the best score and step number. If the score has improved then save a model copy for this pair. Although we will stop on the global score (average across scores over all pairs) we save these models if we want a model that performs the best on a single pair.
                            max_individual_sbleu[dev_idx][1] = sbleu
                            max_individual_sbleu_step[dev_idx][1] = curr_eval_step
                            print("New peak reached for", dev_pair,". Saving.")
                            if args.save_intermediate_checkpoints:
                                torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+"."+str(ctr))
                                torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+"."+str(ctr)+".pure_model") ## Pure model without any ddp markers or optimizer info.
                            else:
                                torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu."+dev_pair)
                                torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu."+dev_pair+".pure_model") 
                    ## Global stats
                    sbleu = sum(sbleus)/len(sbleus) ## The global score.
                    global_sbleu_history.append([sbleu, ctr]) ## Update the global score history.
                    print("Global", metric, "score using", scorertool, "after", ctr, "iterations is:", round(sbleu, 2))
                    writer.add_scalar("global bleu/rouge", sbleu, ctr)
                    if sbleu > max_global_sbleu: ## Update the best score and step number. If this has improved then save a copy for the model. Note that this model MAY NOT be the model that gives the best performance for all pairs.
                        max_global_sbleu = sbleu
                        max_global_sbleu_step = curr_eval_step
                        print("New peak reached. Saving.")
                        if args.save_intermediate_checkpoints:
                            torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu.global."+str(ctr))
                            torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu.global."+str(ctr)+".pure_model") ## Pure model without any ddp markers or optimizer info.
                        else:
                            torch.save(checkpoint_dict, CHECKPOINT_PATH+".best_dev_bleu.global")
                            torch.save(model.module.state_dict(), CHECKPOINT_PATH+".best_dev_bleu.global.pure_model") ## Pure model without any ddp markers or optimizer info.
                            ## Copy the global best pure model to the deploy folder.
                            os.system("cp "+CHECKPOINT_PATH+".best_dev_bleu.global.pure_model "+CHECKPOINT_PATH+"_deploy/pytorch_model.bin")
                    if curr_eval_step - max_global_sbleu_step > (args.early_stop_checkpoints + annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step): ## If the global scores have not improved for more than early_stop_checkpoints + some additional checkpoints to wait for till annealing is done then we stop training.
                        if annealing_attempt < args.max_annealing_attempts: ## We will only downscale the LR a fixed number of times. Each time we downscale the number of checkpoints to wait for declaring convergence will increase by a fixed value.
                            annealing_attempt += 1
                            curr_lr = scheduler.get_lr()[0]
                            print("LR before annealing is:", curr_lr)
                            while scheduler.get_lr()[0] > (curr_lr/args.learning_rate_scaling): ## Currently we down scale the LR by advancing the scheduler by some steps. Now this is a bad idea because the scheduler may reach maximum number of steps where the LR is 0. However the training loop will continue and nothing will be updated. The loophole I have used is to set the maximum number of steps to a large value. Thus far I have not seen a case where this has a bad effect but users who do not trust this part of the code should not use annealing.
                                scheduler.step()
                            print("LR after annealing is:", scheduler.get_lr()[0])

                        else: ## Convergence has been reached and we stop and report the final metrics.
                            print("We have seemingly converged as", metric, "failed to increase for the following number of checkpoints:", args.early_stop_checkpoints+annealing_attempt*args.additional_early_stop_checkpoints_per_anneal_step, ". You may want to consider increasing the number of tolerance steps, doing additional annealing or having a lower peak learning rate or something else.")
                            print("Terminating training")
                            print("Global dev", metric, "history:", [[round(x,2), y] for x,y in global_sbleu_history])
                            print("Individual", metric, "history:", [[lang_pair, [[round(x,2), y] for x,y in individual_sbleu_info_for_language]] for lang_pair, individual_sbleu_info_for_language in individual_sbleu_history])
                            with open(args.model_path + ".quitflag", "w") as f:
                                f.write("1")
                    curr_eval_step += 1

                    model.train() ## Put the model back in training mode where dropout will be done.

                else: ## If no evaluation will be done then I consider it prudent to save the model every 10000 checkpoints by default. Change this to whatever value you want.
                    if ctr % args.no_eval_save_every == 0:
                        print("No evaluation based early stopping so saving every", args.no_eval_save_every, "checkpoints.")
                        checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                        if args.save_intermediate_checkpoints:
                            torch.save(checkpoint_dict, CHECKPOINT_PATH+"."+str(ctr))
                            torch.save(model.module.state_dict(), CHECKPOINT_PATH+"."+str(ctr)+".pure_model")
                        else:
                            torch.save(checkpoint_dict, CHECKPOINT_PATH)
                            torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model")
                print("Saving the model")
                sys.stdout.flush()
                # All processes should see same parameters as they all start from same
                # random parameters and gradients are synchronized in backward passes.
                # Therefore, saving it in one process is sufficient.
                checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
                torch.save(checkpoint_dict, CHECKPOINT_PATH) ## Save a model by default every eval_every steps. This model will be saved with the same file name each time.
                torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model")
                

            # Use a barrier() to make sure that process 1 loads the model after process
            # 0 saves it.
            dist.barrier()
            with open(args.model_path + ".quitflag", "r") as f:
                if f.read().strip() == "1":
                    print("All processess to die!")
                    break
            # configure map_location properly
            print("Loading from checkpoint")
            sys.stdout.flush()
            map_location = {'cuda:%d' % 0: 'cuda:%d' % gpu}
            checkpoint_dict = torch.load(CHECKPOINT_PATH, map_location=map_location)
            model.load_state_dict(checkpoint_dict['model'])
            optimizer.load_state_dict(checkpoint_dict['optimizer'])
            scheduler.load_state_dict(checkpoint_dict['scheduler'])
            del checkpoint_dict
            torch.cuda.empty_cache()
            
        dist.barrier()
        if args.cross_distillation or args.multi_source: ## The returned input ids and input masks are actually a list of two items each. The first item is to be fed to the parent model and the second item is to be fed to the child model.
            input_ids_parent=input_ids[1]
            input_ids=input_ids[0]
            input_ids_parent = input_ids_parent.to(gpu) ## Move to gpu
            input_masks_parent=input_masks[1]
            input_masks=input_masks[0]
            input_masks_parent = input_masks_parent.to(gpu) ## Move to gpu
        
        if args.num_domains_for_domain_classifier > 1: ## The label will contain the label as well as the domain indicator
            domain_classifier_labels=labels[1] ## This is not a tensor yet
            domain_classifier_labels = torch.tensor(domain_classifier_labels, dtype=torch.int64).to(gpu) ## Move to gpu
            labels=labels[0]
            label_mask = labels.eq(tok.pad_token_id).unsqueeze(-1).to(gpu)
        if args.prompt_tuning:
            input_shape = input_masks.size()
            encoder_pad = torch.ones(input_shape[0], args.num_prompts).clone().detach()
            input_masks = torch.cat([encoder_pad, input_masks], dim=1)
        input_ids=input_ids.to(gpu) ## Move to gpu
        input_masks=input_masks.to(gpu) ## Move to gpu
        decoder_input_ids=decoder_input_ids.to(gpu) ## Move to gpu
        labels=labels.to(gpu) ## Move to gpu
        if num_batches_this_optimizer_step == 0: ## If this is the first batch then we need to initialize the optimizer.
            optimizer.zero_grad(set_to_none=True) ## Empty the gradients before any computation.
        if rank == 0:
            writer.add_scalar("learning rate", scheduler.get_lr()[0], ctr)
        if args.mixed_wait_k:
            model.module.config.wait_k = random.randint(1, args.wait_k)
            if rank == 0:
                writer.add_scalar("mixed wait k value", model.module.config.wait_k, ctr)

        if args.fp16: ## The difference between AMP and FP32 is the use of the autocast. The code below is duplicated and can be shrunk. TODO.
            with torch.cuda.amp.autocast():
                mod_compute = model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation, additional_input_ids=input_ids_parent if args.multi_source else None, additional_input_ids_mask=input_masks_parent if args.multi_source else None, label_mask=label_mask if args.num_domains_for_domain_classifier > 1 else None) ## Run the model and get logits. 
                logits = mod_compute.logits
                lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## Softmax tempering of logits if needed.
                loss = label_smoothed_nll_loss(
                    lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
                ) ## Label smoothed cross entropy loss.
                loss = loss*args.softmax_temperature ## Up scale loss in case of non unitary temperatures. Note that in case of self calibrating temperature, the softmax temperature must be set to 1.
                if rank == 0:
                    writer.add_scalar("pure cross entropy loss", loss.detach().cpu().numpy(), ctr)
                if args.ewc_importance != 0: ## Update the model with the EWC loss.
                    ewc_loss_current = args.ewc_importance * ewc_loss.penalty(model)
                    if rank == 0:
                        writer.add_scalar("EWC loss", ewc_loss_current.detach().cpu().numpy(), ctr)
                    loss = loss + ewc_loss_current
                if args.temperature_calibration: 
                    loss = loss*mod_compute.softmax_temperature
                    if rank == 0:
                        writer.add_scalar("calibrated temperature", mod_compute.softmax_temperature.detach().cpu().numpy(), ctr)
                        writer.add_scalar("calibrated temperature loss", loss.detach().cpu().numpy(), ctr)
                if args.num_domains_for_domain_classifier > 1: ## We augment the main loss with the domain classifier loss
                    domain_classifier_logits = mod_compute.domain_classifier_logits
                    domain_classifier_lprobs = torch.nn.functional.log_softmax(domain_classifier_logits, dim=-1) ## Softmax tempering of logits if needed.
                    domain_classifier_loss = label_smoothed_nll_loss(
                        domain_classifier_lprobs.view(-1,args.num_domains_for_domain_classifier), domain_classifier_labels.view(-1,1), args.label_smoothing
                    ) ## Label smoothed cross entropy loss. We are not going to do any temperature related stuff to this.
                    loss = domain_classifier_loss*args.domain_classifier_loss_weight + loss * (1.0-args.domain_classifier_loss_weight)
                    if rank == 0:
                        writer.add_scalar("domain classifier loss", domain_classifier_loss.detach().cpu().numpy(), ctr)
                        writer.add_scalar("loss with domain classifier loss", loss.detach().cpu().numpy(), ctr)
                ## We will do multilayer softmaxing without any consideration for entropy maximization or distillation.
                if mod_compute.additional_lm_logits is not None:
                    for additional_logits in mod_compute.additional_lm_logits:
                        lprobs = torch.nn.functional.log_softmax(additional_logits, dim=-1) ## Softmax tempering of logits if needed.
                        loss_extra = label_smoothed_nll_loss(
                            lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
                        ) ## Label smoothed cross entropy loss.
                        loss_extra = loss_extra*args.softmax_temperature ## Up scale loss in case of non unitary temperatures. Note that in case of self calibrating temperature, the softmax temperature must be set to 1. TODO: Perhaps log this too.
                        if args.temperature_calibration: 
                            loss_extra = loss_extra*mod_compute.softmax_temperature
                        loss += loss_extra ## Up scale loss in case of non unitary temperatures. TODO: Perhaps log this too.
                if args.max_ent_weight != -1: ## This deals with softmax entropy maximization. The logic is that we compute the softmax entropy of the predictions via -(P(Y/X)*log(P(Y/X))). We then add it to the cross entropy loss with a negative sign as we wish to maximize entropy. This should penalize overconfident predictions. 
                    assert (args.max_ent_weight >= 0 and args.max_ent_weight <= 1)
                    logits = logits*args.softmax_temperature ## We have to undo the tempered logits else our entropy estimate will be wrong.
                    if args.temperature_calibration: 
                        logits = logits*mod_compute.softmax_temperature
                    lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## No tempering here
                    entropy = -(torch.exp(lprobs)*lprobs).mean()
                    if rank == 0:
                        writer.add_scalar("softmax entropy", entropy.detach().cpu().numpy(), ctr)
                    if mod_compute.additional_lm_logits is not None:
                        for additional_logits in mod_compute.additional_lm_logits: ## Compute entropy for each layer as well
                            additional_logits = additional_logits*args.softmax_temperature ## We have to undo the tempered logits else our entropy estimate will be wrong.
                            if args.temperature_calibration: 
                                additional_logits = additional_logits*mod_compute.softmax_temperature
                            lprobs = torch.nn.functional.log_softmax(additional_logits, dim=-1) ## No tempering here
                            entropy_extra = -(torch.exp(lprobs)*lprobs).mean()
                            entropy += entropy_extra
                    loss = loss*(1-args.max_ent_weight) - entropy*args.max_ent_weight ## Maximize the entropy so a minus is needed. Weigh and add losses as required.
                    if rank == 0:
                        writer.add_scalar("loss with entropy loss", loss.detach().cpu().numpy(), ctr)
                if args.distillation: ## Time to distill.
                    if args.cross_distillation: ## The input ids and masks should be replaced with those appropriate for the parent.
                        input_ids = input_ids_parent
                        input_masks = input_masks_parent
                    with torch.no_grad(): ## No gradient to avoid memory allocation.
                        parent_mod_compute = parent_model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation) ## Get the parent model's computations.
                    distillation_loss = compute_distillation_losses(mod_compute, parent_mod_compute, labels, tok.pad_token_id, args) ## Compute distillation losses.
                    loss = args.distillation_loss_weight*distillation_loss + (1.0 - args.distillation_loss_weight)*loss ## Update the main loss with weighing and adding.
                    if rank == 0:
                        writer.add_scalar("distillation loss", distillation_loss.detach().cpu().numpy(), ctr)
                        writer.add_scalar("final loss", loss.detach().cpu().numpy(), ctr)
                if args.use_moe or args.moe_adaptors: ## add MOE losses too.
                    moe_loss = torch.sum(torch.stack(mod_compute.encoder_moe_losses)) + torch.sum(torch.stack(mod_compute.decoder_moe_losses))
                    if rank == 0:
                        writer.add_scalar("moe loss", moe_loss.detach().cpu().numpy(), ctr)
                    loss += moe_loss
        else:
            mod_compute = model(input_ids=input_ids, attention_mask=input_masks, decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation, additional_input_ids=input_ids_parent if args.multi_source else None, additional_input_ids_mask=input_masks_parent if args.multi_source else None, label_mask=label_mask if args.num_domains_for_domain_classifier > 1 else None) ## Run the model and get logits.
            logits = mod_compute.logits
            lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## Softmax tempering of logits if needed.
            loss = label_smoothed_nll_loss(
                lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
            ) ## Label smoothed cross entropy loss.
            loss = loss*args.softmax_temperature ## Up scale loss in case of non unitary temperatures.
            if rank == 0:
                writer.add_scalar("pure cross entropy loss", loss.detach().cpu().numpy(), ctr)
            if args.ewc_importance != 0: ## Update the model with the EWC loss.
                ewc_loss_current = args.ewc_importance * ewc_loss.penalty(model)
                if rank == 0:
                    writer.add_scalar("EWC loss", ewc_loss_current.detach().cpu().numpy(), ctr)
                loss = loss + ewc_loss_current
            if args.temperature_calibration: 
                loss = loss*mod_compute.softmax_temperature
                if rank == 0:
                    writer.add_scalar("calibrated temperature", mod_compute.softmax_temperature.detach().cpu().numpy(), ctr)
                    writer.add_scalar("calibrated temperature loss", loss.detach().cpu().numpy(), ctr)
            if args.num_domains_for_domain_classifier > 1: ## We augment the main loss with the domain classifier loss
                domain_classifier_logits = mod_compute.domain_classifier_logits
                domain_classifier_lprobs = torch.nn.functional.log_softmax(domain_classifier_logits, dim=-1) ## Softmax tempering of logits if needed.
                domain_classifier_loss = label_smoothed_nll_loss(
                    domain_classifier_lprobs.view(-1,args.num_domains_for_domain_classifier), domain_classifier_labels.view(-1,1), args.label_smoothing
                ) ## Label smoothed cross entropy loss. We are not going to do any temperature related stuff to this.
                loss = domain_classifier_loss*args.domain_classifier_loss_weight + loss * (1.0-args.domain_classifier_loss_weight)
                if rank == 0:
                    writer.add_scalar("domain classifier loss", domain_classifier_loss.detach().cpu().numpy(), ctr)
                    writer.add_scalar("loss with domain classifier loss", loss.detach().cpu().numpy(), ctr)
            ## We will do multilayer softmaxing without any consideration for distillation or domain classification.
            if mod_compute.additional_lm_logits is not None:
                for additional_logits in mod_compute.additional_lm_logits:
                    lprobs = torch.nn.functional.log_softmax(additional_logits, dim=-1) ## Softmax tempering of logits if needed.
                    loss_extra = label_smoothed_nll_loss(
                        lprobs, labels, args.label_smoothing, ignore_index=tok.pad_token_id
                    ) ## Label smoothed cross entropy loss.
                    loss_extra = loss_extra*args.softmax_temperature ## Up scale loss in case of non unitary temperatures. Note that in case of self calibrating temperature, the softmax temperature must be set to 1. TODO: Perhaps log this too.
                    if args.temperature_calibration: 
                        loss_extra = loss_extra*mod_compute.softmax_temperature
                    loss += loss_extra ## Up scale loss in case of non unitary temperatures. TODO: Perhaps log this too.
            if args.max_ent_weight != -1: ## This deals with softmax entropy maximization. The logic is that we compute the softmax entropy of the predictions via -(P(Y/X)*log(P(Y/X))). We then add it to the cross entropy loss with a negative sign as we wish to maximize entropy. This should penalize overconfident predictions. 
                assert (args.max_ent_weight >= 0 and args.max_ent_weight <= 1)
                logits = logits*args.softmax_temperature ## We have to undo the tempered logits else our entropy estimate will be wrong.
                if args.temperature_calibration: 
                    logits = logits*mod_compute.softmax_temperature
                lprobs = torch.nn.functional.log_softmax(logits, dim=-1) ## No tempering here
                entropy = -(torch.exp(lprobs)*lprobs).mean()
                if rank == 0:
                    writer.add_scalar("softmax entropy", entropy.detach().cpu().numpy(), ctr)
                if mod_compute.additional_lm_logits is not None:
                    for additional_logits in mod_compute.additional_lm_logits: ## Compute entropy for each layer as well
                        additional_logits = additional_logits*args.softmax_temperature ## We have to undo the tempered logits else our entropy estimate will be wrong.
                        if args.temperature_calibration: 
                            additional_logits = additional_logits*mod_compute.softmax_temperature
                        lprobs = torch.nn.functional.log_softmax(additional_logits, dim=-1) ## No tempering here
                        entropy_extra = -(torch.exp(lprobs)*lprobs).mean()
                        entropy += entropy_extra
                loss = loss*(1-args.max_ent_weight) - entropy*args.max_ent_weight ## Maximize the entropy so a minus is needed. Weigh and add losses as required.
                if rank == 0:
                    writer.add_scalar("loss with entropy loss", loss.detach().cpu().numpy(), ctr)
            if args.distillation: ## Time to distill.
                if args.cross_distillation: ## The input ids and masks should be replaced with those appropriate for the parent.
                    input_ids = input_ids_parent
                    input_masks = input_masks_parent
                with torch.no_grad(): ## No gradient to avoid memory allocation.
                    parent_mod_compute = parent_model(input_ids=input_ids, attention_mask=input_masks ,decoder_input_ids=decoder_input_ids, output_hidden_states=args.distillation, output_attentions=args.distillation) ## Get the parent model's computations.
                distillation_loss = compute_distillation_losses(mod_compute, parent_mod_compute, labels, tok.pad_token_id, args) ## Compute distillation losses.
                loss = args.distillation_loss_weight*distillation_loss + (1.0 - args.distillation_loss_weight)*loss ## Update the main loss with weighing and adding.
                if rank == 0:
                    writer.add_scalar("distillation loss", distillation_loss.detach().cpu().numpy(), ctr)
                    writer.add_scalar("final loss", loss.detach().cpu().numpy(), ctr)
            if args.use_moe or args.moe_adaptors: ## add MOE losses too.
                moe_loss = torch.sum(torch.stack(mod_compute.encoder_moe_losses)) + torch.sum(torch.stack(mod_compute.decoder_moe_losses))
                if rank == 0:
                    writer.add_scalar("moe loss", moe_loss.detach().cpu().numpy(), ctr)
                loss += moe_loss

        del input_ids ## Delete to avoid retention.
        del input_masks ## Delete to avoid retention.
        del decoder_input_ids ## Delete to avoid retention.
        del labels ## Delete to avoid retention.
        if args.cross_distillation or args.multi_source:
            del input_ids_parent ## Delete to avoid retention.
            del input_masks_parent ## Delete to avoid retention.
        
        if ctr % 100 == 0 and rank  % 8 == 0:
            fwd_memory = round(torch.cuda.memory_allocated(gpu)/(1024**3), 2)

        ## Optimization part of the model from this point forward.
        if args.fp16: ## The gradient scaler needs to be invoked with FP16/AMP computation. ## With FP16/AMP computation we need to unscale gradients before clipping them. We then optimize and update the scaler.
            loss = loss/args.multistep_optimizer_steps
            scaler.scale(loss).backward()
            num_batches_this_optimizer_step += 1
            losses += loss.detach().cpu().numpy()
            if num_batches_this_optimizer_step < args.multistep_optimizer_steps:
                continue
            if args.max_gradient_clip_value != 0.0:
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            scaler.step(optimizer)
            scaler.update()
            current_scale_value = scaler.get_scale()
            # If the scale value changed then print it.
            if current_scale_value != scale_value:
                print("Gradient scale value changed from {} to {}".format(scale_value, current_scale_value))
                scale_value = current_scale_value

        else: ## With FP32, we just do regular backpropagation, gradient clipping and then step the optimizer.
            loss = loss/args.multistep_optimizer_steps
            loss.backward()
            num_batches_this_optimizer_step += 1
            losses += loss.detach().cpu().numpy()
            if num_batches_this_optimizer_step < args.multistep_optimizer_steps:
                continue
            if args.max_gradient_clip_value != 0.0:
                torch.nn.utils.clip_grad_norm_(model.parameters(), args.max_gradient_clip_value)
            optimizer.step()
        scheduler.step() ## Advance the scheduler to get to the next value of LR.
        lv = losses ## Detach the loss in order to report it.
        losses = 0
        num_batches_this_optimizer_step = 0
        if ctr % 100 == 0 and rank  % 8 == 0: ## Print the current loss every 10 batches but only for the master/prime process.
            bwd_memory=round(torch.cuda.memory_allocated(gpu)/(1024**3), 2)
            end = time.time()
            print(ctr, round(lv.item(),2), round(end-start, 2), "seconds for 100 batches. Memory used post forward / backward passes:", fwd_memory, "/", bwd_memory, "GB.")
            start = time.time()
            sys.stdout.flush()
        
        if ctr % args.eval_every == 0 and rank == 0 and args.save_weights_and_gradeint_info: ## Save the model weight and gradient info every time this condition is triggered.
            for param_name, param_value in model.named_parameters():
                if not ("embed_positions" in param_name and args.positional_encodings):
                    writer.add_histogram("weights."+param_name, param_value.detach().cpu().numpy(), ctr)
                    writer.add_histogram("gradients."+param_name, param_value.grad.detach().cpu().numpy(), ctr)
                
        ctr += 1
        del mod_compute, loss
    
    if rank == 0:
        CHECKPOINT_PATH = args.model_path
        print("Saving the model after the final step")
        # All processes should see same parameters as they all start from same
        # random parameters and gradients are synchronized in backward passes.
        # Therefore, saving it in one process is sufficient.
        if not args.no_eval:
            print("The best",metric, "using", scorertool,"was:", round(max_global_sbleu, 2))
            print("The corresponding step was:", max_global_sbleu_step*args.eval_every)
        checkpoint_dict = {'model': model.state_dict(), 'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(), 'ctr': ctr}
        torch.save(checkpoint_dict, CHECKPOINT_PATH) ## Save one last time.
        torch.save(model.module.state_dict(), CHECKPOINT_PATH+".pure_model") ## Pure model without any ddp markers or optimizer info.
        if args.no_eval: # Save the last checkpoint to the deploy folder.
            os.system("cp "+CHECKPOINT_PATH+".pure_model "+CHECKPOINT_PATH+"_deploy/pytorch_model.bin")
    dist.barrier() ## Wait till all processes reach this point so that the prime process saves the final checkpoint.
    dist.destroy_process_group() ## Everything that has a beginning has an end, Neo!
    

def run_demo():
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--nodes', default=1,
                        type=int, metavar='N')
    parser.add_argument('-g', '--gpus', default=1, type=int,
                        help='number of gpus per node')
    parser.add_argument('-nr', '--nr', default=0, type=int,
                        help='ranking within the nodes')
    parser.add_argument('-a', '--ipaddr', default='localhost', type=str, 
                        help='IP address of the main node')
    parser.add_argument('-p', '--port', default='26023', type=str, 
                        help='Port main node')
    parser.add_argument('--freeze_embeddings', action='store_true', 
                        help='Should freeze embeddings during fine tuning?')
    parser.add_argument('--freeze_encoder', action='store_true', 
                        help='Should we freeze encoder during fine tuning?')
    parser.add_argument('--freeze_exception_list', default=None, type=str, help='Comma separated list of types of params NOT to freeze. The reason I provide a list of params not to freeze is because I want to freeze as many params as possible and that list may be long. This is in the spirit of minimal parameter modification. For prompt tuning it can be "prompt_params,encoder_attn,layer_norm,layernorm". For adaptor tuning it can be "adaptor_params,encoder_attn,layer_norm,layernorm". For both it can be "prompt_params,adaptor_params,encoder_attn,layer_norm,layernorm". Simply passing "decoder" will freeze all except decoder params. By that logic passing "encoder" will freeze all except encoder params. By default this is None so you dont freeze anything. You will have to look at the names of the model params in modeling_mbart.py to get a better idea of what to freeze.')
    parser.add_argument('--positional_encodings', action='store_true', 
                        help='If true then we will use positional encodings instead of learned positional embeddings.')
    parser.add_argument('--no_embed_norm', action='store_true', 
                        help='If true then we wont normalize embeddings.')
    parser.add_argument('--scale_embedding', action='store_true', 
                        help='Should we scale embeddings?')
    parser.add_argument('--no_scale_attention_embedding', action='store_true', 
                        help='Should we scale attention embeddings?')
    parser.add_argument('--adam_8bit', action='store_true', 
                        help='Should we use 8-bit ADAM?')
    parser.add_argument('--multistep_optimizer_steps', default=1, type=int, help="In case you want to simulate a larger batch you should set this to a higher value.")
    parser.add_argument('--encoder_layers', default=6, type=int, help="The value for number of encoder layers")
    parser.add_argument('--decoder_layers', default=6, type=int, help="The value for number of decoder layers")
    parser.add_argument('--label_smoothing', default=0.1, type=float, help="The value for label smoothing")
    parser.add_argument('--weight_decay', default=0.0001, type=float, help="The value for weight decay")
    parser.add_argument('--init_std', default=0.02, type=float, help="The standard deviation of the initial weights")
    parser.add_argument('--lr', default=7e-4, type=float, help="The value for the learning rate")
    parser.add_argument('--init_scale', default=65536.0, type=float, help="FP16 gradient scaler's initial value.")
    parser.add_argument('--adam_eps', default=1e-9, type=float, help="The value for the learning rate")
    parser.add_argument('--layerdrop', default=0.0, type=float, help="The value for layerdrop which indicates the probability that a whole layer will be bypassed via an identity transformation.")
    parser.add_argument('--dropout', default=0.1, type=float, help="The value for embedding dropout")
    parser.add_argument('--attention_dropout', default=0.1, type=float, help="The value for attention dropout")
    parser.add_argument('--activation_dropout', default=0.1, type=float, help="The value for activation dropout")
    parser.add_argument('--data_sampling_temperature', default=5.0, type=float, help="The value for the data sampling temperature")
    parser.add_argument('--token_masking_lambda', default=3.5, type=float, help="The value for the poisson sampling lambda value")
    parser.add_argument('--token_masking_probs_range', nargs='+', type=float, default=[0.3], help="The range of probabilities with which the token will be masked. If you want a fixed probability then specify one argument else specify ONLY 2.")
    parser.add_argument('--repetition_penalty', default=1.0, type=float, 
                        help='To prevent repetition during decoding. 1.0 means no repetition. 1.2 was supposed to be a good value for some settings according to some researchers.')
    parser.add_argument('--no_repeat_ngram_size', default=0, type=int, 
                        help='N-grams of this size will never be repeated in the decoder. Lets play with 2-grams as default.')
    parser.add_argument('--length_penalty', default=1.0, type=float, 
                        help='Set to more than 1.0 for longer sentences.')
    parser.add_argument('--no_skip_special_tokens', action='store_false', 
                        help='Should we return outputs without special tokens? We may need this to deal with situations where the user specified control tokens must be in the output.')
    parser.add_argument('--encoder_no_repeat_ngram_size', default=0, type=int, 
                        help='N-gram sizes to be prevented from being copied over from encoder. Lets play with 2-grams as default.')
    parser.add_argument('--encoder_tying_config', default=None, type=str, 
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--decoder_tying_config', default=None, type=str,
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--gradient_checkpointing', action='store_true', 
                        help='Should we do gradient checkpointing during training? If yes, then the encoder and decoder layer activations will be recomputed during backprop.')
    parser.add_argument('--softmax_temperature', default=1.0, type=float, help="The value for the softmax temperature")
    parser.add_argument('--distillation_temperature', default=1.0, type=float, help="The value for the softmax temperature during distillation")
    parser.add_argument('--temperature_calibration', action='store_true', 
                        help='Are we calibrating the temperature automatically during training? If yes then the softmax_temperature parameter should have a value of 1.0 furthermore the returned temperature will be used to scale the loss.')
    parser.add_argument('--encoder_attention_heads', default=8, type=int, help="The value for number of encoder attention heads")
    parser.add_argument('--decoder_attention_heads', default=8, type=int, help="The value for number of decoder attention heads")
    parser.add_argument('--wait_k', default=-1, type=int, help="The value for k in wait-k snmt. Keep as -1 for non-snmt aka vanilla NMT.")
    parser.add_argument('--mixed_wait_k', action='store_true', 
                        help='Should we train using up to wait_k? This can help simulate multiple wait_k')
    parser.add_argument('--additional_source_wait_k', default=-1, type=int, help="The value for k in wait-k snmt. Keep as -1 for non-snmt aka vanilla NMT. This is the wait-k for the additional source language. Can be used for simultaneous mutlisource NMT.")
    parser.add_argument('--future_prediction', action='store_true', 
                        help='This assumes that we dont mask token sequences randomly but only after the latter half of the sentence. We do this to make the model more robust towards missing future information. Granted we can achieve this using wait-k but methinks this may be a better way of training.')
    parser.add_argument('--unidirectional_encoder', action='store_true', 
                        help='This assumes that we use a unidirectional encoder. This is simulated via a lower-triangular matrix mask in the encoder. Easy peasy lemon squeazy.')
    parser.add_argument('--no_positional_encoding_encoder', action='store_true', 
                        help='This assumes that we dont use positional encodings for encoder')
    parser.add_argument('--no_positional_encoding_decoder', action='store_true', 
                        help='This assumes that we dont use positional encodings for decoder')
    parser.add_argument('--decoder_ffn_dim', default=2048, type=int, help="The value for decoder ff hidden dim")
    parser.add_argument('--encoder_ffn_dim', default=2048, type=int, help="The value for encoder ff hidden dim")
    parser.add_argument('--d_model', default=512, type=int, help="The value for model hidden size")
    parser.add_argument('--embed_low_rank_dim', default=0, type=int, help="The value for the low rank size of the embedding matrix. If 0 then no low rank embedding is used")
    parser.add_argument('--eval_every', default=1000, type=int, help="The number of iterations after which an evaluation must be done. Also saves a checkpoint every these number of steps.")
    parser.add_argument('--no_eval_save_every', default=10000, type=int, help="The number of iterations after which a model must be force saved in case evaluation is not done.")
    parser.add_argument('--max_gradient_clip_value', default=1.0, type=float, help="The max value for gradient norm value")
    parser.add_argument('--use_official_pretrained', action='store_true', 
                        help='Use this flag if you want the argument "pretrained_model" to specify a pretrained model created by someone else. The actual model parameters will be overwritten if you specified locally_fine_tuned_model_path. This is hacky so sue me.')
    parser.add_argument('--locally_fine_tuned_model_path', default=None, type=str, 
                        help='In case you fine-tuned an official model and have a local checkpoint you want to further train or if the run crashed then specifiy it here. If you did not fine-tune an official model but did your own thing then specify it using --pretrained_model argument. Dont bother looking at this if you are not dealing with officially pretrained models.')
    parser.add_argument('--use_official_pretrained_tokenizer', action='store_true', 
                        help='Use this flag if you want the argument "tokenizer_name_or_path" to specify a pretrained tokenizer created by someone else which is usually going to be a part of an official pre-trained model as well.')
    parser.add_argument('--pretrained_model', default='', type=str, 
                        help='Path to the pretrained model.')
    parser.add_argument('--no_reload_optimizer_ctr_and_scheduler', action='store_true',
                        help='Should we reload the optimizer, counter and secheduler? By default we always reload these. Set this to False if we only want to reload the model params and optimize from scratch.')
    parser.add_argument('-m', '--model_path', default='pytorch.bin', type=str, 
                        help='Path to save the fine tuned model')
    parser.add_argument('--save_intermediate_checkpoints', action='store_true', 
                        help='Use this flag if you want intermediate best checkpoints to be saved. If so then numbers will be attached to the checkpoints.')
    parser.add_argument('--warmup_steps', default=16000, type=int,
                        help='Scheduler warmup steps')
    parser.add_argument('--batch_size', default=2048, type=int, 
                        help='Train batch sizes in tokens')
    parser.add_argument('--batch_size_indicates_lines', action='store_true', 
                        help='Should we batch as a fixed number of lines?')
    parser.add_argument('--dev_batch_size', default=1024, type=int, 
                        help='Dev batch sizes in lines')
    parser.add_argument('--sorted_batching', action='store_true', 
                        help='Use this flag if you want to sort the corpus by target length before batching. This helps reduce the number of padding tokens substatially.')
    parser.add_argument('--max_src_length', default=256, type=int, 
                        help='Maximum token length for source language')
    parser.add_argument('--max_tgt_length', default=256, type=int, 
                        help='Maximum token length for target language')
    parser.add_argument('--early_stop_checkpoints', default=10, type=int, 
                        help='Number of checkpoints to wait to see if BLEU increases.')
    parser.add_argument('--learning_rate_scaling', default=2, type=int, 
                        help='How much should the LR be divided by during annealing?. Set num_batches to a larger value or else you will see lr go to zero too soon.')
    parser.add_argument('--max_annealing_attempts', default=2, type=int, 
                        help='Number of times LR should be annealed.')
    parser.add_argument('--additional_early_stop_checkpoints_per_anneal_step', default=5, type=int, 
                        help='How many additional checkpoints should we wait till declaring convergence? This will be multiplied with the annealing step number.')
    parser.add_argument('--num_batches', default=500000, type=int, 
                        help='Number of batches to train on')
    parser.add_argument('--max_eval_batches', default=1000, type=int, 
                        help='These many evaluation batches will be considered. Use a small value like 5 to cover a portion of the evaluation data.')
    parser.add_argument('--max_decode_length_multiplier', default=2.0, type=float, 
                        help='This multiplied by the source sentence length will be the maximum decoding length. If you want to directly specify a particular value then set this to the negative of that value.')
    parser.add_argument('--min_decode_length_multiplier', default=0.1, type=float, 
                        help='This multiplied by the source sentence length will be the minimum decoding length. If you want to directly specify a particular value then set this to the negative of that value.')
    parser.add_argument('--tokenizer_name_or_path', default='ai4bharat/indic-bert', type=str, 
                        help='Name of or path to the tokenizer')
    parser.add_argument('--pretrained_tokenizer_name_or_path', default=None, type=str, 
                        help='Name of or path to the tokenizer of the pretrained model if its different from the current model. This tokenizer will be used for remapping embeddings so as to reuse as many pretrained embeddings as possible.')
    parser.add_argument('--multi_source_method', default=None, type=str, 
                        help='How to merge representations from multiple sources? Should be one of self_relevance_and_merge_after_attention, self_relevance_and_merge_before_attention, merge_after_attention, merge_before_attention, average_softmaxes, self_relevance_and_merge_after_attention_with_context_relevance_only, merge_after_attention_with_context_relevance_only, additional_source_attention, bottleneck_mid_fusion_merge_after_attention, bottleneck_mid_fusion_merge_before_attention, mid_fusion_merge_after_attention, mid_fusion_merge_before_attention. We also need to implement averaging methods such as early averaging (average encoder representations) and late averaging (average softmaxes). Relevance mechanisms should have a separate flag in the future.')
    parser.add_argument('--mid_fusion_layers', default=3, type=int, help='How many additional layers to use for mid-fusion? If N is the desired total number of encoder layers and if the number of pre-fusion encoder layers is M the number of mid-fusion layers should be N-M.')
    parser.add_argument('--bottleneck_mid_fusion_tokens', default=4, type=int, help='How many bottleneck tokens should be used for mid fusion? The non bottleneck version simply concatenates two sequences but the bottleneck version concatenates the bottleneck to each sequence and assumes that the two sequences interact via the bottleneck.')
    parser.add_argument('--tokenization_sampling', action='store_true', 
                        help='Should we use stoachastic tokenization aka BPE dropout or Subword regularization?')
    parser.add_argument('--tokenization_nbest_list_size', type=int, default=64, 
                        help='The size of the nbest list when doing stochastic tokenization.')
    parser.add_argument('--tokenization_alpha_or_dropout', type=float, default=0.1, 
                        help='The value of sentence piece regularization amount controlled via alpha or the amount of BPE dropout controlled by dropout.')
    parser.add_argument('--train_slang', default='en', type=str, 
                        help='Source language(s) for training. If you want to specify the domain of the language pair then specify it as language-domain (hyphen in the middle) and make sure to set --num_domains_for_domain_classifier to a value > 1. If you want to specify an additional source then you need to do the same thing but note that you can do multi-source domain classification as its just too much.')
    parser.add_argument('--train_tlang', default='hi', type=str, 
                        help='Target language(s) for training')
    parser.add_argument('--supported_languages', default=None, type=str, 
                        help='Supported languages or language pairs. This will only be used if you plan to use the interface to the model. If you want to use the model directly then you can ignore this. The format will be a comma separated list of src_language-src_language_token-tgt_language-tgt_language_token. So in the case of IndicBART fine tuned for Hindi-English you would specify Hindi-<2hi>-English-<2en>. In the case of mBART50 for Hindi-English you would specify Hindi-hi_IN-English-en_XX.')
    parser.add_argument('--activation_function', default='gelu', type=str, 
                            help='Activation function. gelu is default. We can use relu or others.')
    parser.add_argument('--train_src', default='', type=str, 
                        help='Source language training sentences')
    parser.add_argument('--train_tgt', default='', type=str, 
                        help='Target language training sentences')
    parser.add_argument('--dev_slang', default='en', type=str, 
                        help='Source language(s) for training')
    parser.add_argument('--dev_tlang', default='hi', type=str, 
                        help='Target language(s) for training')
    parser.add_argument('--dev_src', default='', type=str, 
                        help='Source language(s) development sentences')
    parser.add_argument('--dev_tgt', default='', type=str, 
                        help='Target language(s) development sentences')
    parser.add_argument('--fp16', action='store_true', 
                        help='Should we use fp16 training?')
    parser.add_argument('--no_eval', action='store_true', 
                        help='Should we skip evaluation?')
    parser.add_argument('--source_masking_for_bilingual', action='store_true', 
                        help='Should we use masking on source sentences when training on parallel corpora?')
    parser.add_argument('--is_summarization', action='store_true', 
                        help='Should we use masking on source sentences when training on parallel corpora?')
    parser.add_argument('--span_prediction', action='store_true', 
                        help='This assumes that we do span prediction during pre-training like mt5 and MASS instead of full sentence prediction like mBART.')
    parser.add_argument('--span_to_sentence_prediction', action='store_true', 
                        help='This assumes that we do span to sentence prediction during pre-training the reverse of mt5 and MASS instead of full sentence prediction like mBART.')
    parser.add_argument('--hard_truncate_length', default=1024, type=int, 
                        help='Should we perform a hard truncation of the batch? This will be needed to eliminate cuda caching errors for when sequence lengths exceed a particular limit. This means self attention matrices will be massive and I used to get errors. Choose this value empirically.')
    parser.add_argument('--use_rouge', action='store_true', 
                        help='Should we use ROUGE for evaluation?')
    parser.add_argument('--use_m2', action='store_true', 
                        help='Should we use M2 score for evaluation?')
    parser.add_argument('--max_ent_weight', type=float, default=-1.0, 
                        help='Should we maximize softmax entropy? If the value is anything between 0 and 1 then yes. If its -1.0 then no maximization will be done.')
    parser.add_argument('--ewc_importance', type=float, default=0.0, 
                        help='Should we do elastic weight consolidation? If the value is 0 then we dont do any EWC else we use this as the importance weight in the part "NLL LOSS + ewc_importance*ewc_loss(model,datasetiterator)".')
    parser.add_argument('--ewc_samples', type=int, default=200, 
                        help='How many batches of training data should we run on to do EWC.')
    parser.add_argument('--use_dev_for_fisher', action='store_true', 
                        help='Should we use the dev set for the fisher matrix?')
    parser.add_argument('--use_denoising_prediction_for_fisher', action='store_true', 
                        help='Should we use the denoising objective to compute the fisher matrix?')
    parser.add_argument('--num_domains_for_domain_classifier', type=int, default=1, 
                        help='If we have multiple domains then we should set this to a value higher than one.')
    parser.add_argument('--gradient_reversal_for_domain_classifier', action='store_true', 
                        help='Should we do gradient reversal for the domain classifier? If true then all gradients below the softmax layer (meaning linear projection plus softmax activation) for the classifier will be reversed. Essentially, the representations for two domains will be forced to become more similar. This may in turn be used for style transfer.')
    parser.add_argument('--domain_classifier_loss_weight', type=float, default=0.1, 
                        help='What weight should we give to the domain classifier? 1 minus this weight will be given to the main loss.')
    parser.add_argument('--shard_files', action='store_true', 
                        help='Should we shard the training data? Set to true only if the data is not already pre-sharded.')
    parser.add_argument('--multi_source', action='store_true', 
                        help='Are we doing multisource NMT? In that case you should specify the train_src as a hyphen separated pair indicating the parent language and the child language. You should also ensure that the source file is a tab separated file where each line contains "the parent pair source sentence[tab]child pair source sentence".')
    parser.add_argument('--multilayer_softmaxing', default=None, 
                        help='Should we apply a softmax for each decoder layer? Unsupported for distillation. Only for vanilla training. You have to specify a comma separated list of the intermediate layers which you want to softmax. These go from 0 for the embedding layer to L-2 for the penultimate layer.')
    parser.add_argument('--remap_encoder', default='', type=str, 
                        help='This indicates the remappings for the layer. Example: 1-2,2-4,3-6. The plan is to use these remappings to cut down the model prior to decoding or training. Suppose we have a 6 layer model but we only want to utilize the 2nd, 4th and 6th layer then we will copy the content of the 2nd, 4th and 6th layers to the 1st, 2nd and 3rd layer and delete the former layers from the parameter dictionary. This counts as layer pruning. IMPORTANT NOTE: Ensure that you specify ALL child layer indices you wish mapped. For example if you want 1-2,2-1,3-3 you MUST NOT skip the 3-3 part else it will be deleted from the model dictionary and will be randomly initialized. The loading mechanism is not strict so it will ignore missing or non matching keys. ADDITIONAL NOTE: Load a checkpoint with only the model and not the optimizer to prevent failure as we are not sure if remapping optimizers and learning rate schedulers make sense or not.')
    parser.add_argument('--remap_decoder', default='', type=str, 
                        help='This indicates the remappings for the layer. Example: 1-2,2-4,3-6. The plan is to use these remappings to cut down the model prior to decoding or training. Suppose we have a 6 layer model but we only want to utilize the 2nd, 4th and 6th layer then we will copy the content of the 2nd, 4th and 6th layers to the 1st, 2nd and 3rd layer and delete the former layers from the parameter dictionary. This counts as layer pruning. IMPORTANT NOTE: Ensure that you specify ALL child layer indices you wish mapped. For example if you want 1-2,2-1,3-3 you MUST NOT skip the 3-3 part else it will be deleted from the model dictionary and will be randomly initialized. The loading mechanism is not strict so it will ignore missing or non matching keys. ADDITIONAL NOTE: Load a checkpoint with only the model and not the optimizer to prevent failure as we are not sure if remapping optimizers and learning rate schedulers make sense or not.')
    parser.add_argument('--eliminate_encoder_before_initialization', action='store_true', 
                        help='Lets wipe out the encoder params from the pretrained model before we use it to initialize the current model. This means we have random encoder initialization.')
    parser.add_argument('--eliminate_decoder_before_initialization', action='store_true', 
                        help='Lets wipe out the decoder params from the pretrained model before we use it to initialize the current model. This means we have random decoder initialization.')
    parser.add_argument('--eliminate_embeddings_before_initialization', action='store_true', 
                        help='Lets wipe out the embedding params from the pretrained model before we use it to initialize the current model. This means we have random embedding initialization.')
    ### Distillation flags
    parser.add_argument('--distillation', action='store_true', 
                        help='Should we perform distillation from a parent model? If so then you must specify the model using "parent_pretrained_model". There are several distillation options check the flag called "distillation_styles".')
    parser.add_argument('--cross_distillation', action='store_true', 
                        help='Should we perform cross distillation from a parent model which has been trained on another source language but the same target language? If so then you must specify the model using "parent_pretrained_model". Additionally you should specify the train_src as a hyphen separated pair indicating the parent language and the child language. You should also ensure that the source file is a tab separated file where each line contains "the parent pair source sentence[tab]child pair source sentence" There are several distillation options check the flag called "distillation_styles".')
    parser.add_argument('--use_official_parent_pretrained', action='store_true', 
                        help='Use this flag if you want the argument "pretrained_model" to specify a pretrained model created by someone else for the purposes of distillation. Use this carefully because if the parent is created by someone else then you have to have your own model with different configurations for fine-tuning. Essentially you must make sure that use_official_parent_pretrained and use_official_pretrained are not true simultaneously.')
    parser.add_argument('--parent_pretrained_model', default='', type=str, 
                        help='Path to the parent pretrained model for distillation. The pretrained_model flag will be used to initialize the child model.')
    parser.add_argument('--distillation_loss_weight', type=float, default=0.7, 
                        help='All the distillation losses will be averaged and then multiplied by this weight before adding it to the regular xentropy loss which will be weighted by (1- distillation_loss_weight).')
    parser.add_argument('--distillation_styles', default='cross_entropy', type=str, 
                        help='One or more of softmax_distillation, attention_distillation, hidden_layer_regression. For attention distillation you must make sure that the number of attention heads between the parent and child are the same and for hidden layer regression you must make sure that the hidden size (d_model) is the same for the parent and child. In both these cases, you should also specify the layer mapping. See the "distillation_layer_mapping" flag.')
    parser.add_argument('--distillation_layer_mapping', default='1-1,2-2,3-3,4-4,5-5,6-6', type=str, 
                        help='This indicates the mappings between the parent and child model. The same flag is used for the encoder and the decoder. If you want to map the 2nd parent layer to the first child layer then use 2-1. Note that the layers are not zero indexed as per the description. Ensure that your indices are correct because checking is not done at the moment. If you get weird results then first make sure that your flags are correctly set. If the parent has 6 layers and the child has 3 layers then something like 6-4 will definitely throw an error. User beware! Dokuro mark.')
    parser.add_argument('--parent_encoder_layers', default=6, type=int, help="The value for number of encoder layers")
    parser.add_argument('--parent_decoder_layers', default=6, type=int, help="The value for number of decoder layers")
    parser.add_argument('--parent_dropout', default=0.1, type=float, help="The value for embedding dropout")
    parser.add_argument('--parent_attention_dropout', default=0.1, type=float, help="The value for attention dropout")
    parser.add_argument('--parent_activation_dropout', default=0.1, type=float, help="The value for activation dropout")
    parser.add_argument('--parent_encoder_attention_heads', default=8, type=int, help="The value for number of encoder attention heads")
    parser.add_argument('--parent_decoder_attention_heads', default=8, type=int, help="The value for number of decoder attention heads")
    parser.add_argument('--parent_decoder_ffn_dim', default=2048, type=int, help="The value for decoder ff hidden dim")
    parser.add_argument('--parent_encoder_ffn_dim', default=2048, type=int, help="The value for encoder ff hidden dim")
    parser.add_argument('--parent_d_model', default=512, type=int, help="The value for model hidden size")
    parser.add_argument('--save_weights_and_gradeint_info', action='store_true', 
                        help='Saving gradient information is time consuming. We should make this optional.')
    parser.add_argument('--use_moe', action='store_true', 
                        help='Should we use mixtures of experts instead of regular FFNs?".')
    parser.add_argument('--num_experts', default=8, type=int, help="How many MOE experts should we use?")
    parser.add_argument('--expert_ffn_size', default=128, type=int, help="What is the hidden size of the MOE?")
    parser.add_argument('--prompt_tuning', action='store_true', 
                        help='Should we use continuous prompts and tune them?')
    parser.add_argument('--prompt_dropout', default=0.1, type=float, help="The value for prompt dropout")
    parser.add_argument('--prompt_projection_hidden_size', default=4096, type=int, help="What is the hidden size of the FFN for the prompt embedding projection?")
    parser.add_argument('--prompt_init_std', default=0.02, type=float, help="The value of the standard deviation for the prompt embedding and FFN initialization")
    parser.add_argument('--layernorm_prompt_projection', action='store_true', 
                        help='Should we use layernorm for the input of the FFN that does prompt projection?')
    parser.add_argument('--no_projection_prompt', action='store_true', 
                        help='Should we directly use prompt embeddings as they are instead of using an FFN to project them first? This means prompts, which are embeddings will be directly optimized.')
    parser.add_argument('--use_tanh_activation_prompt', action='store_true', 
                        help='Should  we use the tanh activation or the gelu activation by default?')
    parser.add_argument('--residual_connection_prompt', action='store_true', 
                        help='Should we add the prompt embedding to the output of the projection?')
    parser.add_argument('--initialize_prompts_with_random_embeddings', action='store_true', 
                        help='Should we use initialize the prompts with random embeddings?')
    parser.add_argument('--num_prompts', default=100, type=int, help="How many prompts should we use?")
    parser.add_argument('--recurrent_projections', default=1, type=int, help="How many recurrent projections of the prompt should we do? This means that the output will go through the FFN recurrent_projections number of times?")
    parser.add_argument('--adaptor_tuning', action='store_true', 
                        help='Should we use lightweight adaptors? (Only applied to the final layer)')
    parser.add_argument('--deep_adaptor_tuning', action='store_true', 
                        help='Should we use deep lightweight adaptors? (Applied to each layer)')
    parser.add_argument('--deep_adaptor_tuning_ffn_only', action='store_true', 
                        help='Should we use deep lightweight adaptors? (Applied to each FFN layer)')
    parser.add_argument('--adaptor_dropout', default=0.1, type=float, help="The value for adaptor dropout")
    parser.add_argument('--parallel_adaptors', action='store_true', 
                        help='Should we use parallel adaptors instead of sequential ones?')
    parser.add_argument('--layernorm_adaptor_input', action='store_true', 
                        help='Should we use add a layernorm to the adaptors input?')
    parser.add_argument('--adaptor_scaling_factor', default=1.0, type=float, help="How much should we multiply the adaptor outputs by to control it?")
    parser.add_argument('--residual_connection_adaptor', action='store_true', 
                        help='Should we use a residual or a skip connection for the adaptor as well?')
    parser.add_argument('--encoder_adaptor_tying_config', default=None, type=str, 
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--decoder_adaptor_tying_config', default=None, type=str,
                        help='What should be the parameter tying configuration? 1-1-1-1-1-1 means 6 layers where all are shared. 1-1-2-2-3-3 means 6 layers, 3 unique layers and each one is recurred twice before passing to another layer. 1-2-3-1-2-3 means 6 layers, 3 unique layers and recurrence is done twice after all layers have been passed through. The default None implies a 1-2-3-4-...-N setup')
    parser.add_argument('--adaptor_hidden_size', default=512, type=int, help="What is the hidden size of the adaptor FFNs?")
    parser.add_argument('--moe_adaptors', action='store_true', 
                        help='Should we use mixtures of experts as adaptors?')
    parser.add_argument('--num_moe_adaptor_experts', default=4, type=int, help="How many experts should we use for adaptor FFNs?")
    parser.add_argument('--hypercomplex', action='store_true', 
                        help='Should we use hypercomplex adaptors?')
    parser.add_argument('--hypercomplex_n', default=2, type=int, help="What is the scaling factor for hypercomplex params?")
    parser.add_argument('--ia3_adaptors', action='store_true', 
                        help='Should we use ia3 adaptors from https://arxiv.org/pdf/2205.05638.pdf?')                    
    parser.add_argument('--softmax_bias_tuning', action='store_true', help="Should we use softmax bias tuning to adapt the bias of the softmax?")
    ###
    ### Placeholder flags to prevent code from breaking. These flags are not intended to be used for fine tuning. These flags are here because the common_utils.py methods assume the existence of these args for when joint mbart training and regular NMT training is done. TODO: Modify code to avoid the need for these flags in this script.
    parser.add_argument('--unify_encoder', action='store_true', 
                        help='Should we minimize the encoder representation distances instead of regular cross entropy minimization on the parallel corpus?')
    args = parser.parse_args()
    assert len(args.token_masking_probs_range) <= 2
    print("IP address is", args.ipaddr)
    
    args.world_size = args.gpus * args.nodes                #
    
    train_files = []
    slangs = args.train_slang.strip().split(",")
    tlangs = args.train_tlang.strip().split(",")
    train_srcs = args.train_src.strip().split(",")
    train_tgts = args.train_tgt.strip().split(",")
    if args.num_domains_for_domain_classifier > 1: ## In case we have to do domain classification
        train_domains = args.train_domains.strip().split(",") ## Should not be empty
        args.train_domains = {} ## We can index the domain indicator this way
        domain_idx = 0
        for train_domain in train_domains:
            if train_domain not in args.train_domains:
                args.train_domains[train_domain] = domain_idx
                domain_idx += 1
        train_files = [(slang+"-"+tlang+"-"+train_domain, (train_src, train_tgt, args.train_domains[train_domain])) for slang, tlang, train_src, train_tgt, train_domain in zip(slangs, tlangs, train_srcs, train_tgts, train_domains)]
    else:
        train_files = [(slang+"-"+tlang, (train_src, train_tgt)) for slang, tlang, train_src, train_tgt in zip(slangs, tlangs, train_srcs, train_tgts)]
    print("Training files are:", train_files)
    
    dev_files = []
    if not args.no_eval:
        slangs = args.dev_slang.strip().split(",")
        tlangs = args.dev_tlang.strip().split(",")
        dev_srcs = args.dev_src.strip().split(",")
        dev_tgts = args.dev_tgt.strip().split(",")
        dev_files = [(slang+"-"+tlang, (dev_src, dev_tgt)) for slang, tlang, dev_src, dev_tgt in zip(slangs, tlangs, dev_srcs, dev_tgts)]
    print("Development files are:", dev_files)
    
    os.environ['MASTER_ADDR'] = args.ipaddr              #
    os.environ['MASTER_PORT'] = args.port                      #
    mp.spawn(model_create_load_run_save, nprocs=args.gpus, args=(args,train_files, dev_files))         #
    
if __name__ == "__main__":
    run_demo()
