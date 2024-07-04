import argparse
import os
import math
import ruamel.yaml as yaml
import numpy as np
import random
import time
import datetime
import json
from pathlib import Path

import torch
import torch.backends.cudnn as cudnn
import torch.distributed as dist

import utils
from utils.checkpointer import Checkpointer
from utils.hdfs_io import hmkdir, hexists

from dataset.utils import collect_result
from dataset import create_dataset, create_sampler, create_loader, vqa_collate_fn, build_tokenizer, vd_collate_fn, vd_collate_fn_test

from scheduler import create_scheduler
from optim import create_optimizer


def train(model, data_loader, optimizer, tokenizer, epoch, device, scheduler, config):
    model.train()

    metric_logger = utils.MetricLogger(delimiter="  ")
    metric_logger.add_meter('lr', utils.SmoothedValue(window_size=1, fmt='{value:.6f}'))
    metric_logger.add_meter('loss', utils.SmoothedValue(window_size=1, fmt='{value:.4f}'))

    header = 'Train Epoch: [{}]'.format(epoch)
    print_freq = 50

    accumulate_steps = int(config.get('accumulate_steps', 16))
    
    for i, (image, question, answer, weights, n) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        image, weights = image.to(device, non_blocking=True), weights.to(device, non_blocking=True)
        question_input = tokenizer(question, padding='longest', truncation=True, max_length=config['max_tokens'],
                                   return_tensors="pt").to(device)
        answer_input = tokenizer(answer, padding='longest', return_tensors="pt").to(device)


        loss = model(image, question_input, answer_input, train=True, k=n, weights=weights)

        if accumulate_steps > 1:
            loss = loss / accumulate_steps

            
        # backward
        loss.backward()

        if (i+1) % accumulate_steps == 0:
            # update
            optimizer.step()
            scheduler.step()
            optimizer.zero_grad()

        metric_logger.update(loss=loss.item())
        metric_logger.update(lr=optimizer.param_groups[0]["lr"])

    # gather the stats from all processes
    metric_logger.synchronize_between_processes()
    print("Averaged stats:", metric_logger.global_avg())
    return {k: "{:.5f}".format(meter.global_avg) for k, meter in metric_logger.meters.items()}


@torch.no_grad()
def evaluation(model, data_loader, tokenizer, device, config):
    # test
    model.eval()


    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Generate VQA test result:'
    print_freq = 50

    result = []

    # answer_list = [answer+config['eos'] for answer in data_loader.dataset.answer_list]  # fix bug
    # answer_input = tokenizer(data_loader.dataset.answer_list, padding='longest', return_tensors='pt').to(device)

    for n, (image, question_id, question, answer_options, img_ids, rnd_ids, tmp_n) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        
        image = image.to(device, non_blocking=True)

        question_input = tokenizer(question, padding='longest', return_tensors="pt").to(device)
        # batch size * 100 * seq_len

        answer_input = tokenizer(answer_options[0], padding='longest', return_tensors='pt').to(device)

        topk_ids, topk_probs = model(image, question_input, answer_input, train=False, k=config['k_test'])
        
        _, pred = topk_probs[0].max(dim=0)
       
        tmp_result = {"question_id": question_id[0], "image_id": img_ids[0], "round_id": rnd_ids[0], "topk_ids": topk_ids[0].tolist(), "topk_probs": topk_probs[0].tolist(), "answer": answer_options[0][topk_ids[0][int(pred)]]}
        
        result.append(tmp_result)


    return result


@torch.no_grad()
def evaluation_0(model, data_loader, tokenizer, device, config):
    # test
    model.eval()

    print('-----Evaluation-----')

    metric_logger = utils.MetricLogger(delimiter="  ")
    header = 'Generate VQA test result:'
    print_freq = 50

    result = []

    # answer_list = [answer+config['eos'] for answer in data_loader.dataset.answer_list]  # fix bug
    # answer_input = tokenizer(data_loader.dataset.answer_list, padding='longest', return_tensors='pt').to(device)

    for n, (image, question_id, question, answer_options, img_ids, rnd_ids, tmp_n) in enumerate(metric_logger.log_every(data_loader, print_freq, header)):
        
        image = image.to(device, non_blocking=True)

        question_input = tokenizer(question, padding='longest', return_tensors="pt").to(device)
        # batch size * 100 * seq_len

        answer_inputs = []
        for answer_ops in answer_options:
            answer_input = tokenizer(answer_ops, padding='longest', return_tensors='pt').to(device)
            answer_inputs.append(answer_input)


        new_answer_input = [answer_input.input_ids for answer_input in answer_inputs]

        topk_ids, topk_probs = model(image, question_input, new_answer_input, train=False, k=config['k_test'])
   
        _, pred = topk_probs.max(dim=0)
   
        tmp_result = {"question_id": question_id[0], "image_id": img_ids[0], "round_id": rnd_ids[0], "topk_ids": topk_ids[0].tolist(), "topk_probs": topk_probs[0].tolist(), "answer": answer_options[0][topk_ids[0][int(pred)]]}

        result.append(tmp_result)


    return result


def get_acc(results, test_file):
    # run eval
    preds = {}
    for pred in results:
        preds[int(pred['question_id'])] = pred['answer']

    test_data = []
    if isinstance(test_file, str):
        test_file = [test_file]
    elif not isinstance(test_file, list):
        raise ValueError

    for rpath in test_file:
        with open(rpath, 'r') as f:
            ann = json.load(f)
            if isinstance(ann, list):
                for item in ann:
                    item['answer'] = list(item['label'].keys())[0]
                test_data += ann
            else:
                for k, v in ann.items():
                    v['question_id'] = k
                    v['img_id'] = v.pop('imageId')
                    v['sent'] = v.pop('question')
                    test_data.append(v)

    n, n_correct = 0, 0
    for sample in test_data:
        if 'answer' in sample.keys():
            n += 1
            if preds[int(sample['question_id'])] == sample['answer']:
                n_correct += 1

    print(f"n: {n}, n_correct: {n_correct}, acc: {n_correct / n}", flush=True)
    return n_correct / n if n > 0 else 0

def get_metric(results, test_file):
    preds = {}
    for pred in results:
        preds[int(pred['question_id'])] = pred['topk_ids']

        test_data = []
    if isinstance(test_file, str):
        test_file = [test_file]
    elif not isinstance(test_file, list):
        raise ValueError

    for rpath in test_file:
        with open(rpath, 'r') as f:
            ann = json.load(f)
            if isinstance(ann, list):
                for item in ann:
                    item['answer'] = list(item['label'].keys())[0]
                test_data += ann
            else:
                for k, v in ann.items():
                    v['question_id'] = k
                    v['img_id'] = v.pop('imageId')
                    v['sent'] = v.pop('question')
                    test_data.append(v)

    n, n_1, n_5, n_10 = 0, 0, 0, 0
    sum_mrr = 0.0
    sum_rank = 0
    for sample in test_data:
        if 'answer' in sample.keys():
            n += 1

            gold_rank = sample['ans_opt_ids'].index(int(sample['answer']))

            if gold_rank in preds[int(sample['question_id'])]:
                pred_rank = preds[int(sample['question_id'])].index(gold_rank)
                if pred_rank < 1:
                    n_1 += 1
                if pred_rank < 5:
                    n_5 += 1
                if pred_rank < 10:
                    n_10 += 1 
                sum_mrr += 1 / (pred_rank + 1)
                sum_rank += pred_rank + 1

    print(f"n: {n}, n_1: {n_1}, n_5: {n_5}, n_10: {n_10}, \
          r_1: {n_1 / n}, r_5: {n_5 / n}, r_10: {n_10 / n}, \
          mrr: {sum_mrr / n}, \
          measn_rank: {sum_rank / n}", flush=True)

    return n, n_1, n_5, n_10, sum_mrr / n, sum_rank / n
    

def main(args, config):
    utils.init_distributed_mode(args)
    device = torch.device(args.device)

    world_size = utils.get_world_size()

    if world_size > 8:
        assert hexists(args.output_hdfs) and args.output_hdfs.startswith('hdfs'), "for collect_result among nodes"

    if args.bs > 0:
        config['batch_size_train'] = args.bs // world_size

    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)
    cudnn.benchmark = True

    start_epoch = 0
    max_epoch = config['schedular']['epochs']

    print("Creating vd datasets")

    train_dataset, valid_dataset, test_dataset_dict = create_dataset('vd', config)
    datasets = [train_dataset, valid_dataset]

    train_dataset_size = len(train_dataset)
    world_size = utils.get_world_size()

    if utils.is_main_process():
        print(f"### data {train_dataset_size}, batch size, {config['batch_size_train']} x {world_size}")
        print(f"### Test: {[(k, len(dataset)) for k, dataset in test_dataset_dict.items()]}")

    if args.distributed:
        num_tasks = utils.get_world_size()
        global_rank = utils.get_rank()
        samplers = create_sampler(datasets, [True, False], num_tasks, global_rank)
    else:
        samplers = [None, None]

    train_loader, valid_loader = create_loader(datasets, samplers,
                                               batch_size=[config['batch_size_train'], config['batch_size_test']],
                                               num_workers=[4, 4], is_trains=[True, False],
                                               collate_fns=[vd_collate_fn, vd_collate_fn_test])

    test_loader_dict = {}
    for k, v in test_dataset_dict.items():
        test_loader_dict[k] = create_loader([v], [None], batch_size=[config['batch_size_test']],
                                            num_workers=[4], is_trains=[False], collate_fns=[vd_collate_fn_test])[0]

    print("Creating model")
    tokenizer = build_tokenizer(config['text_encoder'])

    print("### pad_token_id, ", train_dataset.pad_token_id)
    print("### eos_token, ", train_dataset.eos_token)
    config['pad_token_id'] = train_dataset.pad_token_id
    config['eos'] = train_dataset.eos_token

    from models.model_generation import XVLMPlusForVD
    model = XVLMPlusForVD(config=config, tied = True)
    model.load_pretrained(args.checkpoint, config, is_eval=args.evaluate or args.load_vqa_pretrain)
    model = model.to(device)
    print("### Total Params: ", sum(p.numel() for p in model.parameters() if p.requires_grad))

    start_time = time.time()
    print("### output_dir, ", args.output_dir, flush=True)
    print("### output_hdfs, ", args.output_hdfs, flush=True)

    if args.evaluate:
        print("Start IGLUE evaluating")
        for language, test_loader in test_loader_dict.items():
            vqa_result = evaluation(model, test_loader, tokenizer, device, config)
            if language == 'gqa_en':  # no answer
                _ = collect_result(vqa_result, f'vqa_{language}_eval', local_wdir=args.result_dir,
                                   hdfs_wdir=args.output_hdfs, write_to_hdfs=world_size > 8, save_result=True)
            else:
                result = collect_result(vqa_result, f'vqa_{language}_eval', local_wdir=args.result_dir,
                                        hdfs_wdir=args.output_hdfs,
                                        write_to_hdfs=world_size > 8, save_result=False)
                if utils.is_main_process():
                    print(f"Evaluating on {language}", flush=True)
                    n, n_1, n_5, n_10, mrr, mean_rank = get_metric(result, config['test_file'][language][0])
                dist.barrier()

    else:
        print("Start training")
        arg_opt = utils.AttrDict(config['optimizer'])
        optimizer = create_optimizer(arg_opt, model)
        arg_sche = utils.AttrDict(config['schedular'])
        accumulate_steps = int(config.get('accumulate_steps', 16))
        # arg_sche['step_per_epoch'] = math.ceil(train_dataset_size / (config['batch_size_train'] * world_size))
        arg_sche['step_per_epoch'] = math.ceil(train_dataset_size / (config['batch_size_train'] * world_size) / accumulate_steps)
        
        lr_scheduler = create_scheduler(arg_sche, optimizer)

        checkpointer = Checkpointer(args.output_hdfs if hexists(args.output_hdfs) else args.output_dir)

        if args.distributed:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])

        best = 0
        best_epoch = 0
        if 'eval_interval' not in config:
            config['eval_interval'] = 1


        for epoch in range(start_epoch, max_epoch):
            if args.distributed:
                train_loader.sampler.set_epoch(epoch)

            train_stats = train(model, train_loader, optimizer, tokenizer, epoch, device, lr_scheduler, config)

            if utils.is_main_process():
                    
                model_without_ddp = model
                if hasattr(model, 'module'):
                    model_without_ddp = model.module

                save_obj = {
                    'model': model_without_ddp.state_dict(),
                    # 'optimizer': optimizer.state_dict(),
                    # 'lr_scheduler': lr_scheduler.state_dict(),
                    'config': config,
                    # 'epoch': epoch,
                }
                checkpointer.save_checkpoint(model_state=save_obj,
                                                epoch=epoch,
                                                training_states=optimizer.state_dict())

            if epoch == 0 or epoch == 9 or epoch == 4:

                vd_result = evaluation(model, valid_loader, tokenizer, device, config)

                result = collect_result(vd_result, f'vd_valid_epoch{epoch}', local_wdir=args.result_dir,
                                        hdfs_wdir=args.output_hdfs,
                                        write_to_hdfs=world_size > 8, save_result=False)

                if utils.is_main_process():
                    print(f"Evaluating on valid set", flush=True)
                    n, n_1, n_5, n_10, mrr, mean_rank = get_metric(result, config['valid_file'][0])

            if epoch == 9:
                for language, test_loader in test_loader_dict.items():
                    vd_result = evaluation(model, test_loader, tokenizer, device, config)

                    result = collect_result(vd_result, f'vd_{language}_epoch{epoch}', local_wdir=args.result_dir,
                                            hdfs_wdir=args.output_hdfs,
                                            write_to_hdfs=world_size > 8, save_result=False)

                    if utils.is_main_process():
                        print(f"Evaluating on {language}", flush=True)
                        n, n_1, n_5, n_10, mrr, mean_rank = get_metric(result, config['test_file'][language][0])
                    dist.barrier()

                dist.barrier()

        os.system(f"cat {args.output_dir}/log.txt")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('### Time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--checkpoint', type=str, required=True)
    parser.add_argument('--config', default='./configs/vqa2_base.yaml')
    parser.add_argument('--output_dir', default='output/vqa')
    parser.add_argument('--output_hdfs', type=str, default='', help="to collect eval results among nodes")

    parser.add_argument('--device', default='cuda')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--world_size', default=1, type=int, help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')
    parser.add_argument('--distributed', action='store_false')

    parser.add_argument('--bs', default=-1, type=int)
    parser.add_argument('--lr', default=0., type=float)
    parser.add_argument('--fewshot', default='', type=str)
    parser.add_argument('--evaluate', action='store_true')
    parser.add_argument('--load_vqa_pretrain', action='store_true')

    args = parser.parse_args()

    # config = yaml.load(open(args.config, 'r'), Loader=yaml.Loader)

    yaml = yaml.YAML(typ='rt')

    config = yaml.load(open(args.config, 'r'))

    args.result_dir = os.path.join(args.output_dir, 'result')
    hmkdir(args.output_dir)
    hmkdir(args.result_dir)

    if args.lr != 0.:
        config['optimizer']['lr'] = args.lr
        config['schedular']['lr'] = args.lr
    if args.fewshot:
        config['train_file'][0] = config['train_file'][0].format(args.fewshot)
        config['valid_file'][0] = config['valid_file'][0].format(args.fewshot)

    yaml.dump(config, open(os.path.join(args.output_dir, 'config.yaml'), 'w'))

    if len(args.output_hdfs):
        hmkdir(args.output_hdfs)

    main(args, config)
