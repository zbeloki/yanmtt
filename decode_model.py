from transformers import AutoTokenizer
import time

from transformers import MBartForConditionalGeneration, MBartConfig, get_cosine_with_hard_restarts_schedule_with_warmup
from transformers import AdamW

import os

import argparse

os.environ["CUDA_DEVICE_ORDER"]="PCI_BUS_ID"   # see issue #152
#os.environ["CUDA_VISIBLE_DEVICES"]="0,1,2,3,4,5,6,7"

import torch
import torch.nn as nn
from torch.nn.parallel import DistributedDataParallel
import torch.multiprocessing as mp
import sys
import torch.distributed as dist

import random

def generate_batches(tok, args):
    src_file = open(args.test_src)
    curr_batch_count = 0
    encoder_input_batch = []
    max_src_sent_len = 0

    for src_line in src_file:
        start = time.time()
        src_sent = src_line
        lang = "<2"+args.tlang+">"
        src_sent_split = src_sent.split(" ")
        sent_len = len(src_sent_split)
        if sent_len <1 or sent_len > 256:
            src_sent = " ".join(src_sent_split[:256])
        iids = tok(lang + " " + src_sent + " </s>", add_special_tokens=False, return_tensors="pt").input_ids
        curr_src_sent_len = len(iids[0])

        if curr_src_sent_len > max_src_sent_len:
            max_src_sent_len = curr_src_sent_len

        encoder_input_batch.append(lang + " " + src_sent + " </s>")
        curr_batch_count += 1
        if curr_batch_count == args.batch_size:
            input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
            input_masks = input_ids != tok.pad_token_id
            end = time.time()
            #print("Batch generation time:", end-start, "seconds")
            yield input_ids, input_masks
            curr_batch_count = 0
            encoder_input_batch = []
            max_src_sent_len = 0

    if len(encoder_input_batch) != 0:
        input_ids = tok(encoder_input_batch, add_special_tokens=False, return_tensors="pt", padding=True, max_length=max_src_sent_len).input_ids
        input_masks = input_ids != tok.pad_token_id
        yield input_ids, input_masks


def model_create_load_run_save(gpu, args):
    rank = args.nr * args.gpus + gpu
    dist.init_process_group(backend='nccl', init_method='env://', world_size=args.world_size, rank=rank)
    
    tok = AutoTokenizer.from_pretrained("ai4bharat/indic-bert")

    special_tokens_dict = {'additional_special_tokens': ["<s>", "</s>","<2as>", "<2bn>", "<2hi>", "<2en>", "<2gu>", "<2kn>", "<2ml>", "<2mr>", "<2or>", "<2pa>", "<2ta>", "<2te>"]}
    num_added_toks = tok.add_special_tokens(special_tokens_dict)

    #print(tok)
    

    #print(tok.vocab_size) ## Should be 20k

    #print(len(tok)) ## Should be 20k + number of special tokens we added earlier

    print(f"Running DDP checkpoint example on rank {rank}.")
    #setup(rank, world_size)
#    device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
    model = MBartForConditionalGeneration(MBartConfig(vocab_size=len(tok), encoder_layers=6, decoder_layers=6, label_smoothing=0.1))
    model.eval()
    #model = MBartForConditionalGeneration.from_pretrained(args.pretrained_model)
    torch.cuda.set_device(gpu)


    model.cuda(gpu)
#    print(device)

    #print(model.config)
    #print(model.parameters)

    #model = nn.DataParallel(model, device_ids=[0,1,2,3,4,5,6,7], dim=0)
    model = DistributedDataParallel(model, device_ids=[gpu])
    #print(dir(model.module))
    
    map_location = {'cuda:%d' % 0: 'cuda:%d' % rank}
    model.load_state_dict(torch.load(args.model_to_decode))
    
    ctr = 0
    outf = open(args.test_tgt, 'w')
    for input_ids, input_masks in generate_batches(tok, args): #infinite_same_sentence(10000):
        start = time.time()
        #print(input_ids)
        print("Processing batch:", ctr)
        translations = model.module.generate(input_ids.to(gpu), num_beams=args.beam_size, max_length=int(len(input_ids[0])*1.5), early_stopping=True, attention_mask=input_masks.to(gpu), pad_token_id=tok.pad_token_id, eos_token_id=tok(["</s>"]).input_ids[0][1], decoder_start_token_id=tok(["<s>"]).input_ids[0][1], bos_token_id=tok(["<s>"]).input_ids[0][1], length_penalty=args.length_penalty, repetition_penalty=args.repetition_penalty,encoder_no_repeat_ngram_size=args.encoder_no_repeat_ngram_size,no_repeat_ngram_size=args.no_repeat_ngram_size)
        print(len(input_ids), "in and", len(translations), "out")
        for input_id, translation in zip(input_ids, translations):
            translation  = tok.decode(translation, skip_special_tokens=True, clean_up_tokenization_spaces=False) 
            input_id  = tok.decode(input_id, skip_special_tokens=True, clean_up_tokenization_spaces=False) 
            print(input_id, translation)
            #outf.write(input_id + "\t" + translation+"\n")
            outf.write(translation+"\n")
            outf.flush()
    
        
#         except:
#             print("We messed up!")
#             sys.stdout.flush()

        ctr += 1
    outf.close()
    
    dist.destroy_process_group()

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
    parser.add_argument('-m', '--model_to_decode', default='pytorch.bin', type=str, 
                        help='Path to save the fine tuned model')
    parser.add_argument('--batch_size', default=32, type=int, 
                        help='Batch size in terms of number of sentences')
    parser.add_argument('--beam_size', default=4, type=int, 
                        help='Size of beam search')
    parser.add_argument('--repetition_penalty', default=1.5, type=float, 
                        help='To prevent repetition during decoding. 1.0 means no repetition. 1.2 was supposed to be a good value for some settings according to some researchers.')
    parser.add_argument('--no_repeat_ngram_size', default=2, type=int, 
                        help='N-grams of this size will never be repeated in the decoder. Lets play with 2-grams as default.')
    parser.add_argument('--length_penalty', default=1.0, type=float, 
                        help='Set to more than 1.0 for longer sentences.')
    parser.add_argument('--encoder_no_repeat_ngram_size', default=2, type=int, 
                        help='N-gram sizes to be prevented from being copied over from encoder. Lets play with 2-grams as default.')
    parser.add_argument('--slang', default='en', type=str, 
                        help='Source language')
    parser.add_argument('--tlang', default='hi', type=str, 
                        help='Target language')
    parser.add_argument('--test_src', default='', type=str, 
                        help='Source language test sentences')
    parser.add_argument('--test_tgt', default='', type=str, 
                        help='Target language translated sentences')
    args = parser.parse_args()
    print("IP address is", args.ipaddr)
    #########################################################
    args.world_size = args.gpus * args.nodes                #
    os.environ['MASTER_ADDR'] = args.ipaddr              #
    os.environ['MASTER_PORT'] = args.port                      #
    mp.spawn(model_create_load_run_save, nprocs=args.gpus, args=(args,))         #
    #########################################################
#     mp.spawn(demo_fn,
#              args=(args,),
#              nprocs=args.gpus,
#              join=True)
    
if __name__ == "__main__":
    run_demo()