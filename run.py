#%% 
import os
from regex import I
from transformers import AutoTokenizer, AutoModelForCausalLM
from torch.utils.data import DataLoader
from exp_datasets import StreamingQADataset, WebTextDataset, SquadDataset, ArchivalQADataset, RangeSampler, RACEDataset
from weight_model import    CAMeLWeightModel, FcWeightModel, CachedWeightModel, UniformWeightModel, POSWeightModel, BimodalAblation, EntropyThreshold, SSM
from util import set_seed, CACHE_DIR, debug_memory
import csv
from subroutines import qa_eval, weighted_train, qa_ppl_eval, qa_light_tune_early_stop, get_optimizer
import hydra
import wandb
import torch
from tqdm.auto import tqdm
from omegaconf import OmegaConf
from collections import defaultdict
import numpy as np
import functools
import glob
from hydra.utils import to_absolute_path
import uuid
import os
import struct

DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

def get_weight_model(args):
    if args.model_type == 'CaMeLS':
        return FcWeightModel(args, device_=DEVICE)
    elif args.model_type == 'uniform':
        return UniformWeightModel(args, device_=DEVICE)
    elif args.model_type == 'ssm':
        return SSM(tokenizer=args.tokenizer_name, device_=DEVICE)
    else:
        raise NameError('Unknown model type')

def get_base_model(args):   
    base_lm = AutoModelForCausalLM.from_pretrained(args.base_model, cache_dir = CACHE_DIR).to(DEVICE)
    
    if args.base_model_state_dict is not None:
        base_lm.load_state_dict(torch.load(args.base_model_state_dict, map_location=base_lm.device))
    base_lm.train()
    
    #if free all base model layers except embedding/lm head
    # and last args.bm_learned_layers transformer blocks
    #dev note: this is very gpt2 language specific
    if args.bm_learned_layers != -1:
        for param in base_lm.parameters():
            param.requires_grad = False
        for param in base_lm.lm_head.parameters():
            param.requires_grad = True
        for i in range(args.bm_learned_layers):
            for param in base_lm.transformer.h[-1 - i].parameters():
                param.requires_grad = True
    if args.grad_checkpointing:
        print("Enabling Gradient checkpointing")
        base_lm.transformer.gradient_checkpointing = True
           
    return base_lm

def set_eval_batch_params(args):
    key= ''.join(args.base_model.split('/')[-1].split('-')[:-1])
   
    batch_defaults = defaultdict(lambda : 1, {'distilgpt2': 4, 'gpt2': 4, 'gpt2-medium': 2, 'gpt2-large': 1})
    grad_acc_defaults = defaultdict(lambda : 16,{'distilgpt2': 4, 'gpt2': 4, 'gpt2-medium': 8, 'gpt2-large': 16})
    
    if args.batch_size == -1: args.batch_size = batch_defaults[key]
    if args.grad_acc_steps == -1: args.grad_acc_steps = grad_acc_defaults[key]

def set_qa_lt_batch_params(args):
    key=args.base_model.split('qa_models/')[-1]
    if '-retrain' in key:
        key = key.split('-retrain')[0]
   
    batch_defaults = defaultdict(lambda : 2, {'distilgpt2': 32, 'gpt2': 16, 'gpt2-medium': 8, 'gpt2-large': 8, 'gpt2-xl': 4, 'gpt-neo-1.3B': 4})
    
    if args.lt_batch_size == -1: args.lt_batch_size = batch_defaults[key]
    if args.lt_grad_acc_steps == -1: args.lt_grad_acc_steps = 64//args.lt_batch_size

def train(args):
    #Logging into WANDB if needed
    if args.wandb_log:
        wandb.init(config=args, project=args.wandb_project, name=args.wandb_run_name, entity="temporal-lms", settings=wandb.Settings(start_method='fork'))

    os.makedirs(args.log_dir, exist_ok=True)
    os.makedirs(os.path.join(args.log_dir, 'sample_weights'), exist_ok=True)
    os.makedirs(os.path.join(args.log_dir, 'checkpoints'), exist_ok=True)

    with open(os.path.join(args.log_dir, 'config.yaml'), 'w+') as fp:
        OmegaConf.save(config=args, f=fp.name)
        
    weight_model = get_weight_model(args)
    base_lm = get_base_model(args)
    base_state_dict = {k:v.detach().clone().cpu() for k, v in base_lm.state_dict().items()}
    
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name, cache_dir = CACHE_DIR)
    set_seed(args.seed)

    if args.dataset == 'streamingqa':
        train_dataloader = DataLoader( StreamingQADataset(args.train_path, tokenizer=tokenizer), batch_size=args.update_batch_size, shuffle=True)
        val_dataloader = DataLoader( StreamingQADataset(args.val_path, tokenizer=tokenizer,  qa_for_generation=args.val_em), batch_size=args.update_batch_size)
    elif args.dataset == 'squad':
        train_dataloader = DataLoader(SquadDataset(args.train_split, args.train_start_idx, args.train_end_idx, tokenizer=tokenizer), batch_size=args.update_batch_size, shuffle=True)
        val_dataloader = DataLoader(SquadDataset(args.val_split, args.val_start_idx, args.val_end_idx, tokenizer=tokenizer), batch_size=args.update_batch_size, shuffle=True)
    elif args.dataset == 'archivalqa':
        train_dataloader = DataLoader( ArchivalQADataset(args.train_path, tokenizer=tokenizer, full_passage=args.full_passage), batch_size=args.update_batch_size, shuffle=True)
        val_dataloader = DataLoader( ArchivalQADataset(args.val_path, tokenizer=tokenizer, full_passage=args.full_passage,  qa_for_generation=args.val_em), batch_size=args.update_batch_size)
    else:
        raise NotImplementedError(f"Dataset {args.dataset} not implemented")
    sample_train_batch = next(iter(train_dataloader))
    sample_val_batch = next(iter(val_dataloader))

    loc_iters = {}
    loc_dataloaders = {}
    #separate dataloaders for validation so our validation dataloaders do not get shuffled
    val_loc_dataloaders = {}
    if args.c_kl > 0:
        if args.qa_loc:
            if args.dataset == 'streamingqa':
                qa_loc_dataloader =  DataLoader(StreamingQADataset(args.train_path, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=True, drop_last = True)
                qa_loc_val_dataloader =  DataLoader(StreamingQADataset(args.val_path, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=False, drop_last = True)
            elif args.dataset == 'squad':
                qa_loc_dataloader = DataLoader(SquadDataset(args.train_split, args.train_start_idx, args.train_end_idx, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=True, drop_last = True)
                qa_loc_val_dataloader = DataLoader(SquadDataset(args.val_split, args.val_start_idx, args.val_end_idx, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=False, drop_last = True)
            elif args.dataset == 'archivalqa':
                qa_loc_dataloader = DataLoader(ArchivalQADataset(args.train_path, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=True, drop_last = True, full_passage=args.full_passage)
                qa_loc_val_dataloader = DataLoader(ArchivalQADataset(args.val_path, loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size, shuffle=False, drop_last = True, full_passage=args.full_passage)
            else:
                raise NotImplementedError(f"Dataset {args.dataset} not implemented")
            loc_dataloaders['qa'] = qa_loc_dataloader
            val_loc_dataloaders['qa'] = qa_loc_val_dataloader
            loc_iters['qa'] = iter(qa_loc_dataloader)
        if args.web_text_loc:
            web_loc_dataloader = DataLoader(WebTextDataset(loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size,shuffle=True, drop_last = True)
            val_web_loc_dataloader = DataLoader(WebTextDataset(loc = True, tokenizer=tokenizer), batch_size=args.loc_batch_size,shuffle=False, drop_last = True)
            loc_dataloaders['open_web_text'] = web_loc_dataloader
            val_loc_dataloaders['open_web_text'] = val_web_loc_dataloader
            loc_iters['open_web_text'] = iter(web_loc_dataloader)                
    
    w_optimizer = weight_model.get_optimizer(args.outer_lr)
    if args.reduce_lr_on_plateau:
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(w_optimizer, 'min', factor=0.7071, patience=5, verbose=True)
    completed_epochs = 0
    if args.load_checkpoint_path is not None:
        weight_model.load(target_path = args.load_checkpoint_path)
    
    best_val_metrics = {'[AGG]total_loss':1e9, '[AGG]qa_gain':0}
    
    original_inner_lr = args.inner_lr
    if args.sample_inner_lr:
        cur_inner_lr = np.random.uniform(args.min_inner_lr, args.max_inner_lr)
        weight_model.set_inner_lr(cur_inner_lr)
    else:
        cur_inner_lr = args.inner_lr 
    
    if args.reset_base_freq%args.update_batch_size != 0:
        print('reset_base_freq must be divisible by update_batch_size')
        raise NotImplementedError
    batchs_per_base_reset = args.reset_base_freq//args.update_batch_size
    
    for i_epoch in range(completed_epochs, args.n_epochs):
        print('Starting training on epoch',i_epoch)
        w_optimizer.zero_grad()
        metrics_dic = defaultdict(lambda: [])
        set_seed(args.seed*args.n_epochs + i_epoch)
        for i_step, batch in tqdm(enumerate(train_dataloader), desc='training_epoch', position=0, total=len(train_dataloader)):
            #we periodically plot weights on both training and validation data
            if args.sample_weights and (i_step) % (args.sample_steps*args.grad_acc_steps) == 0:
                weight_model.plot_weights(sample_train_batch, tokenizer, f'{os.path.join(args.log_dir, "sample_weights", "train_weights")}{i_epoch}-{i_step}')
                if args.val:
                    weight_model.plot_weights(sample_val_batch, tokenizer, f'{os.path.join(args.log_dir, "sample_weights", "val_weights")}{i_epoch}-{i_step}')
            if args.val and (i_step) % (args.val_steps*args.grad_acc_steps) == 0:
                print('VALIDATING')
                weight_model.eval()
                weight_model.set_inner_lr(original_inner_lr)
                cur_state_dict = {k:v.detach().clone().cpu() for k, v in base_lm.state_dict().items()}
                base_lm.load_state_dict(base_state_dict)
                val_metrics = weight_model.validate(base_lm, val_dataloader, val_loc_dataloaders, reset_base_freq=batchs_per_base_reset, sequential_update=args.sequential_update)
                base_lm.load_state_dict(cur_state_dict)
                weight_model.set_inner_lr(cur_inner_lr)
                weight_model.train()
                if args.wandb_log: wandb.log({'val': val_metrics}, commit = False)
                if val_metrics['[AGG]total_loss'] < best_val_metrics['[AGG]total_loss']:
                    best_val_metrics['[AGG]total_loss'] = val_metrics['[AGG]total_loss']
                    weight_model.save(i_epoch, i_step, file_name = f'best_val_loss-{i_epoch}-{i_step}.pt')
                if val_metrics['[AGG]qa_gain'] > best_val_metrics['[AGG]qa_gain']:
                    best_val_metrics['[AGG]qa_gain'] = val_metrics['[AGG]qa_gain']
                    weight_model.save(i_epoch, i_step, file_name = f'best_qa_gain-{i_epoch}-{i_step}.pt')
                if args.reduce_lr_on_plateau:
                    scheduler.step(val_metrics['[AGG]total_loss'])
                if args.wandb_log: wandb.log({'outer_lr': w_optimizer.param_groups[0]['lr']}, commit = False)
            if args.save_steps and (i_step+1) % (args.save_steps*args.grad_acc_steps) == 0:
                weight_model.save(i_epoch, i_step)

            #loc batchs will be a dictionary containing key-value pairs, where key = locatility dataset name, values are text data  
            loc_batches = {}
            for k in loc_iters.keys():
                try:
                    loc_batches[k] = next(loc_iters[k])
                except StopIteration:
                    loc_iters[k] = iter(loc_dataloaders[k])
                    loc_batches[k] = next(loc_iters[k])

            if args.reset_base_freq <= 1 or (i_step+1) % batchs_per_base_reset == 0:
                outer_loss, metrics,_ = weight_model.step(batch, loc_batches, base_lm = base_lm, sequential_update=args.sequential_update)
            else:
                outer_loss, metrics, updated_lm = weight_model.step(batch, loc_batches, base_lm = base_lm, sequential_update=args.sequential_update)
                #update the base_lm 
                base_lm.load_state_dict(updated_lm.state_dict())
            
            #reset the base_model every reset_base_freq steps
            if args.reset_base_freq > 1:
                if (i_step+1) % batchs_per_base_reset == 0:
                    base_lm.load_state_dict(base_state_dict)
                    if args.sample_inner_lr:
                        cur_inner_lr = np.random.uniform(args.min_inner_lr, args.max_inner_lr)
                        weight_model.set_inner_lr(cur_inner_lr)
            elif args.sample_inner_lr:
                cur_inner_lr = np.random.uniform(args.min_inner_lr, args.max_inner_lr)
                weight_model.set_inner_lr(cur_inner_lr)
            
            for k, v in metrics.items():
                metrics_dic[f'[AGG]{k}'].append(v)
                if args.log_stepwise_metrics:
                    metrics_dic[f'[step-{i_step % args.reset_base_freq }]{k}'].append(v)
        
            outer_loss = outer_loss/args.grad_acc_steps
            outer_loss.backward()
            if (i_step+1) % args.grad_acc_steps == 0:
                grad_norm = torch.nn.utils.clip_grad_norm_(weight_model.parameters(), args.grad_clip_thresh)
                
                w_optimizer.step()
                w_optimizer.zero_grad()
                if args.wandb_log: 
                    wandb.log({'grad_norm': grad_norm, 'inner_lr':cur_inner_lr},commit = False) 
                    wandb.log({'train': {f'{k}': np.mean(v) for k,v in metrics_dic.items()}})
                metrics_dic.clear()

        #at the end of each epoch of training, explicilty step the optimizer, checkpoint the model, and log metrics
        grad_norm = torch.nn.utils.clip_grad_norm_(weight_model.parameters(), args.grad_clip_thresh)   
        w_optimizer.step()
        w_optimizer.zero_grad()
        print('Saving model')
        weight_model.save(i_epoch, -1)
        weight_model.plot_weights(sample_train_batch, tokenizer, f'{os.path.join(args.log_dir,"sample_weights","train")}{i_epoch}-{i_step}')
        if args.val:
            weight_model.plot_weights(sample_val_batch, tokenizer, f'{os.path.join(args.log_dir,"sample_weights" ,"val")}{i_epoch}-{i_step}')
    
    if args.wandb_log:
        wandb.finish()
     
def evaluate(args):

    if not os.path.isdir(args.log_dir):
        os.makedirs(args.log_dir)
        print("created folder : ", args.log_dir)
    else:
        print(args.log_dir, "folder already exists.")
    
    set_eval_batch_params(args)
    set_qa_lt_batch_params(args)
    
    print(f"batch_size:{args.batch_size}, grad_acc_steps:{args.grad_acc_steps}")
    
    if args.wandb_log:
        wandb.init(config=args, project=args.wandb_project, name=args.wandb_run_name, entity="temporal-lms", settings=wandb.Settings(start_method='fork'))

    
    with open(os.path.join(args.log_dir, 'config.yaml'), 'w+') as fp:
        OmegaConf.save(config=args, f=fp.name)

    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    base_lm = get_base_model(args)
    base_lm.to(device)
    
    
    if args.debug:
        debug_memory('base_model loaded')
    
    set_seed(args.seed)
    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer_name, cache_dir = CACHE_DIR)
    if args.dataset == 'streamingqa':
        test_dataset = StreamingQADataset(args.test_path, tokenizer=tokenizer, qa_for_generation=True, pad_qa_for_gen = (args.generation_batch_size != 1), downsample_to=args.downsample_to)
        weighted_train_dataset = test_dataset
        lt_dataset = StreamingQADataset(args.lt_train_path, tokenizer=tokenizer, qa_only=True)
        lt_val_dataset = StreamingQADataset(args.lt_val_path, tokenizer=tokenizer, qa_only=True, qa_for_generation=True, downsample_to=args.downsample_to) 
            
    elif args.dataset == 'squad':
        test_dataset = SquadDataset(args.test_split, args.test_start_idx, args.test_end_idx, tokenizer=tokenizer, qa_for_generation=True, pad_qa_for_gen = (args.generation_batch_size != 1), downsample_to=args.downsample_to)
        weighted_train_dataset = test_dataset.get_deduplicated_dataset()
        lt_dataset = SquadDataset(args.qa_lt_split, args.qa_lt_start_idx, args.qa_lt_end_idx, tokenizer=tokenizer, qa_only=True)
        lt_val_dataset = SquadDataset(args.qa_lt_val_split, args.qa_lt_val_start_idx, args.qa_lt_val_end_idx, tokenizer=tokenizer, qa_only=True, qa_for_generation=True, downsample_to=args.downsample_to)

    elif args.dataset == 'archivalqa':
        test_dataset = ArchivalQADataset(args.test_path, tokenizer=tokenizer, qa_for_generation=True, pad_qa_for_gen = (args.generation_batch_size != 1), downsample_to=args.downsample_to, full_passage=args.full_passage)
        weighted_train_dataset = test_dataset.get_deduplicated_dataset()
        lt_dataset = ArchivalQADataset(args.lt_train_path, tokenizer=tokenizer, qa_only=True)
        lt_val_dataset = ArchivalQADataset(args.lt_val_path, tokenizer=tokenizer, qa_only=True, qa_for_generation=True)
    else:
        print(f'dataset [{args.dataset}] not supported for evaluation')
        raise NotImplementedError
    
    test_dataloader = DataLoader(test_dataset, batch_size=args.generation_batch_size, shuffle=False)
    lt_dataloader = DataLoader(lt_dataset, batch_size=args.lt_batch_size, shuffle=True)
    lt_val_dataloader = DataLoader(lt_val_dataset, batch_size=args.lt_batch_size, shuffle=False)
        
        
    weighted_train_dataloaders = []
    
    if args.eval_every_k == -1:
        args.eval_every_k = len(weighted_train_dataset)
        
    for start in range(0, len(weighted_train_dataset), args.eval_every_k):
        end = min(start + args.eval_every_k, len(weighted_train_dataset))
        weighted_train_dataloaders.append(DataLoader(weighted_train_dataset, batch_size=args.batch_size, sampler=RangeSampler(start, end)))
   
    
    if isinstance(args.eval, str):
        args.eval = [args.eval] 
    
    eval_fns = {}
    for eval_mode in args.eval:
        if eval_mode == 'ppl':
            eval_fns[eval_mode] = (qa_ppl_eval)
        elif eval_mode[:2] == 'em':
            top_k = 1 if eval_mode == 'em' else int(eval_mode[2:])
            num_beams = max(args.num_beams, top_k- top_k%args.num_beam_groups + args.num_beam_groups)
            if num_beams != args.num_beams:
                print(f'overwriting arguement number beam groups of {args.num_beams} to {num_beams}')
            eval_fns[eval_mode] = functools.partial(qa_eval, top_k = top_k, diversity_penalty = args.diversity_penalty, num_beam_groups = args.num_beam_groups, num_beams=num_beams)
        
        else:
            print('unknown evaluation mode:', eval_mode)

    if args.qa_lt_init:
        #hard code a lot of these optimizers
        base_lm = qa_light_tune_early_stop(lt_dataloader, lt_val_dataloader, os.path.join(args.log_dir, f'init_qa_lt'), args.lt_steps, args.lt_val_steps, args.lt_lr, device, model = base_lm, grad_accumulation_steps=args.grad_acc_steps, optimizer = args.optimizer, seed = args.seed, debug=args.debug, early_stop=True, wandb_log=args.wandb_log, grad_clip_thresh=args.grad_clip_thresh, name='init_qa_lt', stopping_metric='nll', stop_k = 5)
        

    if args.eval_init:
        print(f'evaluating init model ')
        base_lm.eval()
        for mode, eval_fn in eval_fns.items():
            eval_fn(test_dataloader, os.path.join(args.log_dir, f'init_{mode}.csv'), model = base_lm)
            
    if args.qa_eval_intermediate:
        print(f'evaluating init model on held out qa')
        base_lm.eval()
        for mode, eval_fn in eval_fns.items():
            eval_fn(lt_val_dataloader, os.path.join(args.log_dir, f'init_qa_val_{mode}.csv'), model = base_lm)
    
    if args.n_epochs > 0:
        print(f'training with learned weights for {args.n_epochs} epochs')
        weight_model = get_weight_model(args)
        if args.model_type == 'CaMeLS':
            weight_model.load(target_path = to_absolute_path(args.weight_model_path))
        weight_model.eval()
        if args.bimodal_ablation:
            weight_model = BimodalAblation(orig_weight_model = weight_model, low_output = args.low_output, high_output=args.high_output)
        
        optimizer_state_dict = None
        for i, wt_dataloader in enumerate(weighted_train_dataloaders):
        #base_lm will be modified in place
            base_lm.requires_grad_(True)
            base_lm.train()
            print('model hash pre:', base_lm.state_dict()['transformer.wte.weight'].sum(), base_lm.state_dict()['transformer.h.0.ln_1.weight'].sum())
            if args.eval_every_k_post != -1:
                os.makedirs(os.path.join(args.log_dir, 'ft'), exist_ok=True)
                base_lm, optimizer_state_dict = weighted_train(weight_model, wt_dataloader, args.n_epochs, args.lr, base_lm, save_dir = os.path.join(args.log_dir, 'ft'), grad_accumulation_steps=args.grad_acc_steps, resume = args.resume, optimizer = args.optimizer, seed = args.seed, debug = args.debug, wandb_log=args.wandb_log, grad_clip_thresh=args.grad_clip_thresh, optimizer_state_dict = optimizer_state_dict, save_steps=args.eval_every_k_post)
            else:
                base_lm, optimizer_state_dict = weighted_train(weight_model, wt_dataloader, args.n_epochs, args.lr, base_lm, save_dir = os.path.join(args.log_dir, 'ft'), grad_accumulation_steps=args.grad_acc_steps, resume = args.resume, optimizer = args.optimizer, seed = args.seed, debug = args.debug, wandb_log=args.wandb_log, grad_clip_thresh=args.grad_clip_thresh, optimizer_state_dict = optimizer_state_dict)
            print('model hash post:', base_lm.state_dict()['transformer.wte.weight'].sum(), base_lm.state_dict()['transformer.h.0.ln_1.weight'].sum())
            if args.qa_lt_intermediate:
                
                k_updates = (i+1)*args.eval_every_k
                base_lm = qa_light_tune_early_stop(lt_dataloader, lt_val_dataloader, os.path.join(args.log_dir, f'post{k_updates}_qa_lt'), args.lt_steps, args.lt_val_steps, args.lt_lr, device, model = base_lm, grad_accumulation_steps=args.grad_acc_steps, optimizer = args.optimizer, seed = args.seed, debug=args.debug, early_stop=args.lt_early_stop, wandb_log=args.wandb_log, grad_clip_thresh=args.grad_clip_thresh, name=f'qa_lt_{k_updates}', stopping_metric=args.lt_stopping_metric)
                
            
            if args.eval_intermediate:
                k_updates = (i+1)*args.eval_every_k
                print(f'evaluating model after {k_updates} updates')
                
                base_lm.eval()
                for mode, eval_fn in eval_fns.items():
                    eval_fn(test_dataloader, os.path.join(args.log_dir, f'post{k_updates}_{mode}.csv'), model = base_lm)
                print('model hash post:', base_lm.state_dict()['transformer.wte.weight'].sum(), base_lm.state_dict()['transformer.h.0.ln_1.weight'].sum())
            if args.qa_eval_intermediate:
                print(f'evaluating model after {k_updates} updates on held out qa')
                base_lm.eval()
                for mode, eval_fn in eval_fns.items():
                    eval_fn(lt_val_dataloader, os.path.join(args.log_dir, f'post{k_updates}_qa_val_{mode}.csv'), model = base_lm)      
            
    if args.debug:
        debug_memory('post_weight_training') 
        
    #light tuning 
    if args.qa_lt_final:
        print("light tuning for qa with early stopping")
        base_lm = qa_light_tune_early_stop(lt_dataloader, lt_val_dataloader, os.path.join(args.log_dir, f'final_qa_lt'), args.lt_steps, args.lt_val_steps, args.lt_lr, device, model = base_lm, grad_accumulation_steps=args.grad_acc_steps, optimizer = args.optimizer, seed = args.seed, debug=args.debug, early_stop=args.lt_early_stop, wandb_log=args.wandb_log, grad_clip_thresh=args.grad_clip_thresh, name=f'final_qa_lt', stopping_metric=args.lt_stopping_metric, stop_k = args.lt_patience, delete_checkpoints=args.delete_checkpoints)
            
    print('evaluating final model')
    base_lm.eval()
    for mode, eval_fn in eval_fns.items():
        eval_fn(test_dataloader, os.path.join(args.log_dir, f'final_{mode}.csv'), model = base_lm)
    
    if args.qa_eval_intermediate:
        print(f'evaluating final model on held out qa')
        base_lm.eval()
        for mode, eval_fn in eval_fns.items():
            eval_fn(lt_val_dataloader, os.path.join(args.log_dir, f'final_qa_val_{mode}.csv'), model = base_lm)  
            
    if args.eval_every_k_post != -1:
        for state_dic_path in glob.glob(os.path.join(args.log_dir, 'ft', '*.pt')):
            base_lm.load_state_dict(torch.load(state_dic_path, map_location=base_lm.device))
            output_path = state_dic_path.replace('.pt', 'eval')
            for mode, eval_fn in eval_fns.items():
                eval_fn(test_dataloader, output_path + f'{mode}.csv', model = base_lm)    
                eval_fn(lt_val_dataloader,  output_path + f'qa_val{mode}.csv', model = base_lm)
            os.remove(state_dic_path)
        
    if args.wandb_log:
        wandb.finish()
#%%



def generate_uuid(digits=6):
    if not hasattr(uuid, "uuid_value"):
        uuid.uuid_value = struct.unpack('I', os.urandom(4))[0] % int(10**digits)

    return uuid.uuid_value

OmegaConf.register_new_resolver("uuid", generate_uuid)
OmegaConf.register_new_resolver(
    "shorten_path", lambda path: path.split('learned_updating/')[-1].replace('/', '-')
)

@hydra.main(config_path='conf', config_name='config')
def run(args):
    for big_model_name in ['gpt2-xl', 'gpt2-neo-1.3B', 'gpt2-neo-2.7B']:
        if big_model_name in args.base_model:
            print(f'using big model {big_model_name}, setting grad_checkpointing to True')
            args.grad_checkpointing = True
    if 'data_dir' in args:
        args.data_dir = to_absolute_path(args.data_dir)
    if 'test_path' in args:
        args.test_path = to_absolute_path(args.test_path)
        
    if args.task == 'train':
        train(args)
    elif args.task == 'eval':
        evaluate(args)



if __name__ == '__main__':
    run()
# %%