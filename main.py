import argparse
import datetime
import json
import random
import time
import os
import os.path as osp
import pickle
# from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader, DistributedSampler
from yacs.config import CfgNode as CN


import datasets
import util.misc as utils
from datasets.ref_data import get_data, collater
from engine import evaluate, train_one_epoch
from models import build_model

# torch.autograd.set_detect_anomaly(True)

def get_args_parser():
    parser = argparse.ArgumentParser('Set transformer detector', add_help=False)
    parser.add_argument('--lr', default=1e-4, type=float)
    parser.add_argument('--lr_backbone', default=1e-5, type=float)
    parser.add_argument('--batch_size', default=18, type=int)
    parser.add_argument('--weight_decay', default=1e-4, type=float)
    parser.add_argument('--epochs', default=40, type=int)
    parser.add_argument('--lr_drop', default=30, type=int)
    parser.add_argument('--clip_max_norm', default=0.1, type=float,
                        help='gradient clipping max norm')

    # Model parameters
    parser.add_argument('--frozen_weights', type=str, default=None,
                        help="Path to the pretrained model. If set, only the mask head will be trained")
    # * Backbone
    parser.add_argument('--backbone', default='resnet50', type=str,
                        help="Name of the convolutional backbone to use")
    parser.add_argument('--dilation', action='store_true',
                        help="If true, we replace stride with dilation in the last convolutional block (DC5)")
    parser.add_argument('--position_embedding', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on top of the image features")
    parser.add_argument('--query_pos', default='sine', type=str, choices=('sine', 'learned'),
                        help="Type of positional embedding to use on attention of words query")
    parser.add_argument('--yolo_path', default=None, type=str,
                        help="Pretrained resnet backbone on yolov3")
    parser.add_argument('--bert_type', default=None, type=str,
                        help='Type of pretrained bert, and none means spacy embedding')

    # * Transformer
    parser.add_argument('--enc_layers', default=6, type=int,
                        help="Number of encoding layers in the transformer")
    parser.add_argument('--enc_lang_layers', default=0, type=int,
                        help="Number of language encoding in the transformer")
    parser.add_argument('--dec_layers', default=6, type=int,
                        help="Number of decoding layers in the transformer")
    parser.add_argument('--dim_feedforward', default=2048, type=int,
                        help="Intermediate size of the feedforward layers in the transformer blocks")
    parser.add_argument('--hidden_dim', default=256, type=int,
                        help="Size of the embeddings (dimension of the transformer)")
    parser.add_argument('--dropout', default=0.1, type=float,
                        help="Dropout applied in the transformer")
    parser.add_argument('--nheads', default=8, type=int,
                        help="Number of attention heads inside the transformer's attentions")
    parser.add_argument('--num_queries', default=100, type=int,
                        help="Number of query slots")
    parser.add_argument('--pre_norm', action='store_true')
    parser.add_argument('--no_cross_encoder', action='store_true',
                        help="Whether to use cross modal encoder or not")
    
    # * Segmentation
    parser.add_argument('--masks', action='store_true',
                        help="Train segmentation head if the flag is provided")

    # Loss
    parser.add_argument('--no_aux_loss', dest='aux_loss', action='store_false',
                        help="Disables auxiliary decoding losses (loss at each layer)")
    parser.add_argument('--use_mlm', action='store_true',
                        help='Use mlm loss during training')
    # * Matcher
    parser.add_argument('--matcher', default='constant', type=str,
                        help="GT and prediction matching stragy")
    parser.add_argument('--set_cost_class', default=1, type=float,
                        help="Class coefficient in the matching cost")
    parser.add_argument('--set_cost_bbox', default=5, type=float,
                        help="L1 box coefficient in the matching cost")
    parser.add_argument('--set_cost_giou', default=2, type=float,
                        help="giou box coefficient in the matching cost")
    # * Loss coefficients
    # parser.add_argument('--mask_loss_coef', default=1, type=float)
    # parser.add_argument('--dice_loss_coef', default=1, type=float)
    parser.add_argument('--bbox_loss_coef', default=5, type=float)
    parser.add_argument('--giou_loss_coef', default=2, type=float)
    parser.add_argument('--eos_coef', default=0.1, type=float,
                        help="Relative classification weight of the no-object class")
    parser.add_argument('--mlm_loss_coef', default=1, type=float,
                        help="Mask language model loss weight for pretraining")
    parser.add_argument('--match_loss_coef', default=1, type=float,
                        help="whether image and text weight of loss")
    parser.add_argument('--obj_att_coef', default=10, type=float,
                        help='Object attention map')
    parser.add_argument('--attr_loss_coef', default=5, type=float,
                        help="Attributes prediction loss")
    parser.add_argument('--rel_loss_coef', default=1, type=float,
                        help='Relationships prediction loss')
    parser.add_argument('--no_img', action='store_true', default=False,
                        help='Not to use image during pretraining')
    parser.add_argument('--no_obj_att', action='store_true', default=False,
                        help='Remove object attention loss during pretraining vg')

    # dataset parameters
    # parser.add_argument('--dataset_file', default='coco')
    parser.add_argument('--ds_name', default='vg',
                        help="For REC task")
    parser.add_argument("--ds_info", default="data/ds_info.json",
                        help="filename of data config")
    # parser.add_argument('--coco_path', type=str)
    # parser.add_argument('--coco_panoptic_path', type=str)
    # parser.add_argument('--remove_difficult', action='store_true')

    parser.add_argument('--output_dir', default='results',
                        help='path where to save, empty for no saving')
    parser.add_argument('--lab_name', default='demo',
                        help='experiment name')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=42, type=int)
    parser.add_argument('--resume', default='', help='resume from checkpoint')
    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true')
    parser.add_argument('--num_workers', default=2, type=int)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--dist_url', default='env://', help='url used to set up distributed training')

    # attention visualization parameters
    parser.add_argument('--visualize_dir', default=None, type=str,
                        help='output directory of attention map, if None, there is not')
    return parser


def main(args):
    utils.init_distributed_mode(args)
    print("git:\n  {}\n".format(utils.get_sha()))

    if args.frozen_weights is not None:
        assert args.masks, "Frozen training is meant for segmentation only"
    print(args)

    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + utils.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    random.seed(seed)

    model, criterion, postprocessors = build_model(args)
    model.to(device)

    model_without_ddp = model
    if args.distributed:
        model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu],\
            find_unused_parameters=True)
        model_without_ddp = model.module
    n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)
    print('number of params:', n_parameters)

    param_dicts = [
        {"params": [p for n, p in model_without_ddp.named_parameters() if "backbone" not in n and p.requires_grad]},
        {
            "params": [p for n, p in model_without_ddp.named_parameters() if "backbone" in n and p.requires_grad],
            "lr": args.lr_backbone,
        },
    ]
    optimizer = torch.optim.AdamW(param_dicts, lr=args.lr,
                                  weight_decay=args.weight_decay)
    lr_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, args.lr_drop)

#    dataset_train = build_dataset(image_set='train', args=args)
#    dataset_val = build_dataset(image_set='val', args=args)
    args.ds_info = CN(json.load(open(args.ds_info)))
    dataset = get_data(args, args.ds_info)
    if args.distributed:
        sampler_train = DistributedSampler(dataset['train'])
        sampler_val = DistributedSampler(dataset['val'], shuffle=False)
    else:
        sampler_train = torch.utils.data.RandomSampler(dataset['train'])
        sampler_val = torch.utils.data.SequentialSampler(dataset['val'])

    batch_sampler_train = torch.utils.data.BatchSampler(
        sampler_train, args.batch_size, drop_last=True)

    data_loader_train = DataLoader(dataset['train'], batch_sampler=batch_sampler_train,
                                   collate_fn=collater, num_workers=args.num_workers)
    data_loader_val = DataLoader(dataset['val'], args.batch_size, sampler=sampler_val,
                                 drop_last=False, collate_fn=collater, num_workers=args.num_workers)

#    if args.dataset_file == "coco_panoptic":
#        # We also evaluate AP during panoptic training, on original coco DS
#        coco_val = datasets.coco.build("val", args)
#        base_ds = get_coco_api_from_dataset(coco_val)
#    else:
#        base_ds = get_coco_api_from_dataset(dataset_val)

    if args.frozen_weights is not None:
        checkpoint = torch.load(args.frozen_weights, map_location='cpu')
        model_without_ddp.detr.load_state_dict(checkpoint['model'])

    if args.resume:
        print("Use resume :", args.resume)
        if args.resume.startswith('https'):
            checkpoint = torch.hub.load_state_dict_from_url(
                args.resume, map_location='cpu', check_hash=True)
        else:
            checkpoint = torch.load(args.resume, map_location='cpu')
        model_without_ddp.load_state_dict(checkpoint['model'], strict=False)
        if not args.eval and 'optimizer' in checkpoint and 'lr_scheduler' in checkpoint and 'epoch' in checkpoint:
            optimizer.load_state_dict(checkpoint['optimizer'])
            lr_scheduler.load_state_dict(checkpoint['lr_scheduler'])
            args.start_epoch = checkpoint['epoch'] + 1

#    if args.eval:
#        test_stats, coco_evaluator = evaluate(model, criterion, postprocessors,
#                                              data_loader_val, base_ds, device, args.output_dir)
#        if args.output_dir:
#            utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
#        return
    
    if args.eval:
        if args.visualize_dir and utils.is_main_process():
            args.visualize_dir = os.path.join(args.output_dir, args.visualize_dir)
            if not os.path.exists(args.visualize_dir):
                os.makedirs(args.visualize_dir)
        test_stats = evaluate(model, criterion, postprocessors, data_loader_val, device, args.output_dir, visualize_dir=args.visualize_dir)
        # utils.save_on_master(coco_evaluator.coco_eval["bbox"].eval, output_dir / "eval.pth")
        return

    print("Start training")
    output_dir = args.output_dir
    start_time = time.time()
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            sampler_train.set_epoch(epoch)
        train_stats = train_one_epoch(
            model, criterion, data_loader_train, optimizer, device, epoch,
            args.clip_max_norm)
        lr_scheduler.step()
        if args.output_dir:
            checkpoint_paths = [osp.join(output_dir,  'checkpoint.pth')]
            # extra checkpoint before LR drop and every 10 epochs
            if (epoch + 1) % args.lr_drop == 0 or (epoch + 1) % 1 == 0:
                checkpoint_paths.append(osp.join(output_dir ,f'checkpoint{epoch:04}.pth'))
            for checkpoint_path in checkpoint_paths:
                utils.save_on_master({
                    'model': model_without_ddp.state_dict(),
                    'optimizer': optimizer.state_dict(),
                    'lr_scheduler': lr_scheduler.state_dict(),
                    'epoch': epoch,
                    'args': args,
                }, checkpoint_path)

        test_stats = evaluate(
            model, criterion, postprocessors, data_loader_val, device, args.output_dir
        )

        log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                     **{f'test_{k}': v for k, v in test_stats.items()},
                     'epoch': epoch,
                     'n_parameters': n_parameters}

        if args.output_dir and utils.is_main_process():
            with open(osp.join(output_dir,"log.txt"), "a") as f:
                f.write(json.dumps(log_stats) + "\n")

    total_time = time.time() - start_time
    total_time_str = str(datetime.timedelta(seconds=int(total_time)))
    print('Training time {}'.format(total_time_str))


if __name__ == '__main__':
    parser = argparse.ArgumentParser('DETR training and evaluation script', parents=[get_args_parser()])
    args = parser.parse_args()
    args.output_dir = os.path.join(args.output_dir, args.ds_name, args.lab_name)
    if utils.is_main_process() and not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir, exist_ok=True)
    main(args)
