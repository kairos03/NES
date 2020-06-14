
import argparse
import time
import logging
from datetime import datetime
from ptflops import get_model_complexity_info
import torch.nn.functional as F
import numpy as np

try:
    from apex import amp
    from apex.parallel import DistributedDataParallel as DDP
    from apex.parallel import convert_syncbn_model
    has_apex = True
except ImportError:
    from torch.nn.parallel import DistributedDataParallel as DDP
    has_apex = False

try:
    import nvidia.dali.plugin.pytorch as plugin_pytorch
    from nvidia.dali.pipeline import Pipeline
    import nvidia.dali.ops as ops
    import nvidia.dali.types as types
except ImportError:
    print("dali not installed...")
    # raise ImportError("Please install DALI from https://www.github.com/NVIDIA/DALI to run this example.")

from timm.data import Dataset, create_loader, resolve_data_config, FastCollateMixup, mixup_target
from timm.models import create_model, resume_checkpoint
from timm.utils import *
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy
from timm.optim import create_optimizer
from timm.scheduler import create_scheduler

import torch
import torch.nn as nn
import torchvision.utils


torch.backends.cudnn.benchmark = True

parser = argparse.ArgumentParser(description='Training')
parser.add_argument('data', metavar='DIR',
                    help='path to dataset')
parser.add_argument('--model', default='resnet101', type=str, metavar='MODEL',
                    help='Name of model to train (default: "countception"')
parser.add_argument('--num-classes', type=int, default=1000, metavar='N',
                    help='number of label classes (default: 1000)')
parser.add_argument('--opt', default='sgd', type=str, metavar='OPTIMIZER',
                    help='Optimizer (default: "sgd"')
parser.add_argument('--opt-eps', default=1e-8, type=float, metavar='EPSILON',
                    help='Optimizer Epsilon (default: 1e-8)')
parser.add_argument('--gp', default='avg', type=str, metavar='POOL',
                    help='Type of global pool, "avg", "max", "avgmax", "avgmaxc" (default: "avg")')
parser.add_argument('--tta', type=int, default=0, metavar='N',
                    help='Test/inference time augmentation (oversampling) factor. 0=None (default: 0)')
parser.add_argument('--pretrained', action='store_true', default=False,
                    help='Start with pretrained version of specified network (if avail)')
parser.add_argument('--img-size', type=int, default=None, metavar='N',
                    help='Image patch size (default: None => model default)')
parser.add_argument('--mean', type=float, nargs='+', default=None, metavar='MEAN',
                    help='Override mean pixel value of dataset')
parser.add_argument('--std', type=float, nargs='+', default=None, metavar='STD',
                    help='Override std deviation of of dataset')
parser.add_argument('--interpolation', default='random', type=str, metavar='NAME',
                    help='Image resize interpolation type (overrides model)')
parser.add_argument('-b', '--batch-size', type=int, default=32, metavar='N',
                    help='input batch size for training (default: 32)')
parser.add_argument('-tb', '--test-batch-size', type=int, default=32, metavar='N',
                    help='input batch size for training (default: 32)')
parser.add_argument('-s', '--initial-batch-size', type=int, default=0, metavar='N',
                    help='initial input batch size for training (default: 0)')
parser.add_argument('--epochs', type=int, default=200, metavar='N',
                    help='number of epochs to train (default: 2)')
parser.add_argument('--start-epoch', default=None, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--decay-epochs', type=int, default=30, metavar='N',
                    help='epoch interval to decay LR')
parser.add_argument('--warmup-epochs', type=int, default=3, metavar='N',
                    help='epochs to warmup LR, if scheduler supports')
parser.add_argument('--decay-rate', '--dr', type=float, default=0.1, metavar='RATE',
                    help='LR decay rate (default: 0.1)')
parser.add_argument('--sched', default='step', type=str, metavar='SCHEDULER',
                    help='LR scheduler (default: "step"')
parser.add_argument('--drop', type=float, default=0.0, metavar='DROP',
                    help='Dropout rate (default: 0.)')
parser.add_argument('--reprob', type=float, default=0., metavar='PCT',
                    help='Random erase prob (default: 0.)')
parser.add_argument('--remode', type=str, default='const',
                    help='Random erase mode (default: "const")')
parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                    help='learning rate (default: 0.01)')
parser.add_argument('--warmup-lr', type=float, default=0.0001, metavar='LR',
                    help='warmup learning rate (default: 0.0001)')
parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                    help='SGD momentum (default: 0.9)')
parser.add_argument('--weight-decay', type=float, default=0.0001,
                    help='weight decay (default: 0.0001)')
parser.add_argument('--mixup', type=float, default=0.0,
                    help='mixup alpha, mixup enabled if > 0. (default: 0.)')
parser.add_argument('--mixup-off-epoch', default=0, type=int, metavar='N',
                    help='turn off mixup after this epoch, disabled if 0 (default: 0)')
parser.add_argument('--smoothing', type=float, default=0.1,
                    help='label smoothing (default: 0.1)')
parser.add_argument('--bn-tf', action='store_true', default=False,
                    help='Use Tensorflow BatchNorm defaults for models that support it (default: False)')
parser.add_argument('--bn-momentum', type=float, default=None,
                    help='BatchNorm momentum override (if not None)')
parser.add_argument('--bn-eps', type=float, default=None,
                    help='BatchNorm epsilon override (if not None)')
parser.add_argument('--model-ema', action='store_true', default=False,
                    help='Enable tracking moving average of model weights')
parser.add_argument('--model-ema-force-cpu', action='store_true', default=False,
                    help='Force ema to be tracked on CPU, rank=0 node only. Disables EMA validation.')
parser.add_argument('--model-ema-decay', type=float, default=0.9998,
                    help='decay factor for model weights moving average (default: 0.9998)')
parser.add_argument('--seed', type=int, default=42, metavar='S',
                    help='random seed (default: 42)')
parser.add_argument('--log-interval', type=int, default=50, metavar='N',
                    help='how many batches to wait before logging training status')
parser.add_argument('--recovery-interval', type=int, default=0, metavar='N',
                    help='how many batches to wait before writing recovery checkpoint')
parser.add_argument('-j', '--workers', type=int, default=4, metavar='N',
                    help='how many training processes to use (default: 1)')
parser.add_argument('--num-gpu', type=int, default=1,
                    help='Number of GPUS to use')
parser.add_argument('--initial-checkpoint', default='', type=str, metavar='PATH',
                    help='path to init checkpoint (default: none)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--save-images', action='store_true', default=False,
                    help='save images of input bathes every log interval for debugging')
parser.add_argument('--amp', action='store_true', default=False,
                    help='use NVIDIA amp for mixed precision training')
parser.add_argument('--sync-bn', action='store_true',
                    help='enabling apex sync BN.')
parser.add_argument('--no-prefetcher', action='store_true', default=False,
                    help='disable fast prefetcher')
parser.add_argument('--output', default='', type=str, metavar='PATH',
                    help='path to output folder (default: none, current dir)')
parser.add_argument('--eval-metric', default='prec1', type=str, metavar='EVAL_METRIC',
                    help='Best metric (default: "prec1"')
parser.add_argument("--local_rank", default=0, type=int)

# compression method
parser.add_argument('--fc_compress', default='fully_fc', type=str, metavar='FCLAYER',
                    help='compression method on fc layer (default: "fully_fc"')
parser.add_argument('--enable_se', action='store_true', default=False,
                    help='enable the se block (default: "False"')
parser.add_argument('--group_se', action='store_true', default=False,
                    help='change se block to be group conv (default: "False"')
parser.add_argument('--sampling', action='store_true', default=False,
                    help='Use WS sampling method (default: "False"')
parser.add_argument('--shortcut_coeff', action='store_true', default=False,
                    help='assign a weight to short connection (default: "False"')

parser.add_argument('--up_sampling_ratio', type=float, default=0.5,
                    help='relative ratio of the first 1x1 layer to the last one (default: 0.5)')
parser.add_argument('--KD_train', action='store_true', default=False,
                    help='use KD to guide training (default: "False"')
parser.add_argument('--KD_alpha', type=float, default=0.9,
                    help='alpha for KD training (default: 0.9)')
parser.add_argument('--KD_temperature', type=float, default=20.0,
                    help='temperature for KD training (default: 20.)')
parser.add_argument('--teacher_step', type=float, default=2.0,
                    help='batch size for teacher network (default: 2.)')

parser.add_argument('--display-info', action='store_true', default=False,
                    help='display the flops per layer (default: "False"')
parser.add_argument('--auto_augment', action='store_true', default=False,
                    help='specify if use auto data augmentation (default: "False"')
parser.add_argument('--mixcut', action='store_true', default=False,
                    help='specify if use auto data augmentation (default: "False"')
parser.add_argument('--cutmix_prob', default=0, type=float,
                    help='cutmix probability')
parser.add_argument('--beta', default=0, type=float,
                    help='hyperparameter beta')
parser.add_argument('--no-args', action='store_true', default=False,
                    help='Use pass args to the model (default: "False"')
parser.add_argument('--eval-only', action='store_true', default=False,
                    help='Use pass args to the model (default: "False"')  
parser.add_argument('--drop_path_prob', type=float, default=0, help='drop path probability')
parser.add_argument('--load_optimizer', action='store_true', default=False,
                    help='decide if to load the optimizer from the check point')

def main():
    setup_default_logging()
    args = parser.parse_args()
    args.prefetcher = not args.no_prefetcher
    args.distributed = False
    print('local rank is:',args.local_rank)
    print(args)
    if 'WORLD_SIZE' in os.environ:
        args.distributed = int(os.environ['WORLD_SIZE']) > 1
        if args.distributed and args.num_gpu > 1:
            logging.warning('Using more than one GPU per process in distributed mode is not allowed. Setting num_gpu to 1.')
            args.num_gpu = 1

    args.device = 'cuda:0'
    args.world_size = 1
    args.rank = 0  # global rank
    if args.distributed:
        args.num_gpu = 1
        args.device = 'cuda:%d' % args.local_rank
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(backend='nccl', init_method='env://')
        args.world_size = torch.distributed.get_world_size()
        args.rank = torch.distributed.get_rank()
    assert args.rank >= 0

    if args.distributed:
        logging.info('Training in distributed mode with multiple processes, 1 GPU per process. Process %d, total %d.'
                     % (args.rank, args.world_size))
    else:
        logging.info('Training with a single process on %d GPUs.' % args.num_gpu)

    torch.manual_seed(args.seed + args.rank)
    if args.no_args:
        model = create_model(
                    args.model,
                    pretrained=args.pretrained,
                    num_classes=args.num_classes,
                    drop_rate=args.drop,
                    global_pool=args.gp,
                    bn_tf=args.bn_tf,
                    bn_momentum=args.bn_momentum,
                    bn_eps=args.bn_eps,
                    drop_connect_rate=0.2,
                    checkpoint_path=args.initial_checkpoint)
    else:
        model = create_model(
            args.model,
            pretrained=args.pretrained,
            num_classes=args.num_classes,
            drop_rate=args.drop,
            global_pool=args.gp,
            bn_tf=args.bn_tf,
            bn_momentum=args.bn_momentum,
            bn_eps=args.bn_eps,
            drop_connect_rate=0.2,
            checkpoint_path=args.initial_checkpoint,
            args = args)
    if args.model_ema:
        model_e = create_model(
            args.model,
            pretrained=args.pretrained,
            num_classes=args.num_classes,
            drop_rate=args.drop,
            global_pool=args.gp,
            bn_tf=args.bn_tf,
            bn_momentum=args.bn_momentum,
            bn_eps=args.bn_eps,
            drop_connect_rate=0.2,
            checkpoint_path=args.initial_checkpoint,
            args = args)
    # import pdb; pdb.set_trace();
    if args.model.startswith('darts'):     
        model.drop_path_prob = 0
    # flops, params = get_model_complexity_info(model.cpu(), (3, 224, 224), as_strings=True, print_per_layer_stat=args.display_info)
    # print('Flops:  ' + flops)
    # print('Params: ' + params)
    # print(model)
    if args.KD_train:
        teacher_model = create_model(
            "efficientnet_b1_dq",
            pretrained=True,
            num_classes=args.num_classes,
            drop_rate=args.drop,
            global_pool=args.gp,
            bn_tf=args.bn_tf,
            bn_momentum=args.bn_momentum,
            bn_eps=args.bn_eps,
            drop_connect_rate=0.2,
            checkpoint_path=args.initial_checkpoint,
            args = args)
        


        # flops_teacher, params_teacher = get_model_complexity_info(teacher_model, (3, 224, 224), as_strings=True, print_per_layer_stat=True)
        # print("Using KD training...")
        # print("FLOPs of teacher model: ", flops_teacher)
        # print("Params of teacher model: ", params_teacher)

    if args.local_rank == 0:
        logging.info('Model %s created, param count: %d' %
                     (args.model, sum([m.numel() for m in model.parameters()])))

    data_config = resolve_data_config(model, args, verbose=args.local_rank == 0)

    # optionally resume from a checkpoint
    start_epoch = 0
    optimizer_state = None
    if args.resume:
        optimizer_state, start_epoch = resume_checkpoint(model, args.resume, args.start_epoch)
        # import pdb;pdb.set_trace()

    if args.num_gpu > 1:
        if args.amp:
            logging.warning(
                'AMP does not work well with nn.DataParallel, disabling. Use distributed mode for multi-GPU AMP.')
            args.amp = False
        model = nn.DataParallel(model, device_ids=list(range(args.num_gpu))).cuda()
        if args.KD_train:
            teacher_model = nn.DataParallel(teacher_model, device_ids=list(range(args.num_gpu))).cuda()
    else:
        model.cuda()
        if args.KD_train:
            teacher_model.cuda()

    optimizer = create_optimizer(args, model)
    # FIXME remove this line later
    # import pdb; pdb.set_trace();
    optimizer_state = None
    if optimizer_state is not None and args.load_optimizer:
        optimizer.load_state_dict(optimizer_state)
        optimizer_state = None

    use_amp = False
    if has_apex and args.amp:
        model, optimizer = amp.initialize(model, optimizer, opt_level='O1')
        use_amp = True
    if args.local_rank == 0:
        logging.info('NVIDIA APEX {}. AMP {}.'.format(
            'installed' if has_apex else 'not installed', 'on' if use_amp else 'off'))

    model_ema = None
    if args.model_ema:
        # Important to create EMA model after cuda(), DP wrapper, and AMP but before SyncBN and DDP wrapper
        # import pdb; pdb.set_trace()
        model_ema = ModelEma(
            model,
            decay=args.model_ema_decay,
            device='cpu' if args.model_ema_force_cpu else '',
            resume=args.resume)

    if args.distributed:
        if args.sync_bn:
            try:
                if has_apex:
                    model = convert_syncbn_model(model)
                else:
                    model = torch.nn.SyncBatchNorm.convert_sync_batchnorm(model)
                if args.local_rank == 0:
                    logging.info('Converted model to use Synchronized BatchNorm.')
            except Exception as e:
                logging.error('Failed to enable Synchronized BatchNorm. Install Apex or Torch >= 1.1')
        if has_apex:
            model = DDP(model, delay_allreduce=True)
        else:
            if args.local_rank == 0:
                logging.info("Using torch DistributedDataParallel. Install NVIDIA Apex for Apex DDP.")
            model = DDP(model, device_ids=[args.local_rank])  # can use device str in Torch >= 1.1
        # NOTE: EMA model does not need to be wrapped by DDP

    lr_scheduler, num_epochs = create_scheduler(args, optimizer)
    if start_epoch > 0:
        lr_scheduler.step(start_epoch)
    if args.local_rank == 0:
        logging.info('Scheduled epochs: {}'.format(num_epochs))

    train_dir = os.path.join(args.data, 'train')
    if not os.path.exists(train_dir):
        logging.error('Training folder does not exist at: {}'.format(train_dir))
        exit(1)
    dataset_train = Dataset(train_dir)

    collate_fn = None
    if args.prefetcher and args.mixup > 0:
        collate_fn = FastCollateMixup(args.mixup, args.smoothing, args.num_classes)

    if args.auto_augment:
        print('using auto data augumentation...')
    loader_train = create_loader(
        dataset_train,
        input_size=data_config['input_size'],
        batch_size=args.batch_size,
        is_training=True,
        use_prefetcher=args.prefetcher,
        rand_erase_prob=args.reprob,
        rand_erase_mode=args.remode,
        interpolation=args.interpolation,  # FIXME cleanly resolve this? data_config['interpolation'],
        mean=data_config['mean'],
        std=data_config['std'],
        num_workers=args.workers,
        distributed=args.distributed,
        collate_fn=collate_fn,
        use_auto_aug=args.auto_augment,
        use_mixcut=args.mixcut,
    )

    eval_dir = os.path.join(args.data, 'val')
    if not os.path.isdir(eval_dir):
        logging.error('Validation folder does not exist at: {}'.format(eval_dir))
        exit(1)
    dataset_eval = Dataset(eval_dir)

    loader_eval = create_loader(
        dataset_eval,
        input_size=data_config['input_size'],
        batch_size=args.test_batch_size,
        is_training=False,
        use_prefetcher=args.prefetcher,
        interpolation=data_config['interpolation'],
        mean=data_config['mean'],
        std=data_config['std'],
        num_workers=args.workers,
        distributed=args.distributed,
    )

    if args.mixup > 0.:
        # smoothing is handled with mixup label transform
        train_loss_fn = SoftTargetCrossEntropy().cuda()
        validate_loss_fn = nn.CrossEntropyLoss().cuda()
    elif args.smoothing:
        train_loss_fn = LabelSmoothingCrossEntropy(smoothing=args.smoothing).cuda()
        validate_loss_fn = nn.CrossEntropyLoss().cuda()
    else:
        train_loss_fn = nn.CrossEntropyLoss().cuda()
        validate_loss_fn = train_loss_fn
    if args.KD_train:
        train_loss_fn = nn.KLDivLoss().cuda()

    eval_metric = args.eval_metric
    best_metric = None
    best_epoch = None
    saver = None
    output_dir = ''
    if args.local_rank == 0:
        output_base = args.output if args.output else './output'
        exp_name = '-'.join([
            datetime.now().strftime("%Y%m%d-%H%M%S"),
            args.model,
            str(data_config['input_size'][-1])
        ])
        output_dir = get_outdir(output_base, 'train', exp_name)
        decreasing = True if eval_metric == 'loss' else False
        saver = CheckpointSaver(checkpoint_dir=output_dir, decreasing=decreasing)

    try:
        # import pdb;pdb.set_trace()
        for epoch in range(start_epoch, num_epochs):
            if args.model.startswith("darts"):
                model.drop_path_prob = args.drop_path_prob * epoch / num_epochs
            if args.distributed:
                loader_train.sampler.set_epoch(epoch)
            if args.KD_train:
                train_metrics = train_epoch(
                    epoch, model, loader_train, optimizer, train_loss_fn, args,
                    lr_scheduler=lr_scheduler, saver=saver, output_dir=output_dir,
                    use_amp=use_amp, model_ema=model_ema, teacher_model = teacher_model)
            else:
                #print('in debug mode...')
                # import pdb; pdb.set_trace()
                #train_metrics = None
                if not args.eval_only:
                    train_metrics = train_epoch(
                       epoch, model, loader_train, optimizer, train_loss_fn, args,
                       lr_scheduler=lr_scheduler, saver=saver, output_dir=output_dir,
                       use_amp=use_amp, model_ema=model_ema)

            # def __init__(self, model, bits_activations=8, bits_parameters=8, bits_accum=32,
            #                 overrides=None, mode=LinearQuantMode.SYMMETRIC, clip_acts=ClipMode.NONE,
            #                 per_channel_wts=False, model_activation_stats=None, fp16=False, clip_n_stds=None,
            #                 scale_approx_mult_bits=None):
            # import distiller
            # import pdb; pdb.set_trace()
            # quantizer = quantization.PostTrainLinearQuantizer.from_args(model, args)
            # quantizer.prepare_model(distiller.get_dummy_input(input_shape=model.input_shape))
            # quantizer = distiller.quantization.PostTrainLinearQuantizer(model, bits_activations=8, bits_parameters=8)
            # quantizer.prepare_model()

            # distiller.utils.assign_layer_fq_names(model)
            # # msglogger.info("Generating quantization calibration stats based on {0} users".format(args.qe_calibration))
            # collector = distiller.data_loggers.QuantCalibrationStatsCollector(model)
            # with collector_context(collector):
            #     eval_metrics = validate(model, loader_eval, validate_loss_fn, args)
            #     # Here call your model evaluation function, making sure to execute only
            #     # the portion of the dataset specified by the qe_calibration argument
            # yaml_path = './dir/quantization_stats.yaml'
            # collector.save(yaml_path)

            eval_metrics = validate(model, loader_eval, validate_loss_fn, args)

            if model_ema is not None and not args.model_ema_force_cpu:
                ema_eval_metrics = validate(model_ema.ema.cuda(), loader_eval, validate_loss_fn, args, log_suffix=' (EMA)')
                eval_metrics = ema_eval_metrics

            if lr_scheduler is not None:
                lr_scheduler.step(epoch, eval_metrics[eval_metric])

            update_summary(
                epoch, train_metrics, eval_metrics, os.path.join(output_dir, 'summary.csv'),
                write_header=best_metric is None)

            if saver is not None:
                # save proper checkpoint with eval metric
                save_metric = eval_metrics[eval_metric]
                best_metric, best_epoch = saver.save_checkpoint(
                    model, optimizer, args,
                    epoch=epoch + 1,
                    model_ema=model_ema,
                    metric=save_metric)

    except KeyboardInterrupt:
        pass
    if best_metric is not None:
        logging.info('*** Best metric: {0} (epoch {1})'.format(best_metric, best_epoch))


def rand_bbox(size, lam):
    W = size[2]
    H = size[3]
    cut_rat = np.sqrt(1. - lam)
    cut_w = np.int(W * cut_rat)
    cut_h = np.int(H * cut_rat)

    # uniform
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    bbx1 = np.clip(cx - cut_w // 2, 0, W)
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = np.clip(cx + cut_w // 2, 0, W)
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2

def train_epoch(
        epoch, model, loader, optimizer, loss_fn, args,
        lr_scheduler=None, saver=None, output_dir='', use_amp=False, model_ema=None, teacher_model = None):

    if args.prefetcher and args.mixup > 0 and loader.mixup_enabled:
        if args.mixup_off_epoch and epoch >= args.mixup_off_epoch:
            loader.mixup_enabled = False

    batch_time_m = AverageMeter()
    data_time_m = AverageMeter()
    losses_m = AverageMeter()

    model.train()
    if args.KD_train:
        teacher_model.eval()

    end = time.time()
    last_idx = len(loader) - 1
    num_updates = epoch * len(loader)
    for batch_idx, (input, target) in enumerate(loader):
        last_batch = batch_idx == last_idx
        data_time_m.update(time.time() - end)
        if not args.prefetcher:
            input = input.cuda()
            target = target.cuda()
            if args.mixup > 0.:
                lam = 1.
                if not args.mixup_off_epoch or epoch < args.mixup_off_epoch:
                    lam = np.random.beta(args.mixup, args.mixup)
                input.mul_(lam).add_(1 - lam, input.flip(0))
                target = mixup_target(target, args.num_classes, lam, args.smoothing)

        r = np.random.rand(1)
        if args.beta > 0 and r < args.cutmix_prob:
            # generate mixed sample
            lam = np.random.beta(args.beta, args.beta)
            rand_index = torch.randperm(input.size()[0]).cuda()
            target_a = target
            target_b = target[rand_index]
            bbx1, bby1, bbx2, bby2 = rand_bbox(input.size(), lam)
            input[:, :, bbx1:bbx2, bby1:bby2] = input[rand_index, :, bbx1:bbx2, bby1:bby2]
            # adjust lambda to exactly match pixel ratio
            lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (input.size()[-1] * input.size()[-2]))
            # compute output
            input_var = torch.autograd.Variable(input, requires_grad=True)
            target_a_var = torch.autograd.Variable(target_a)
            target_b_var = torch.autograd.Variable(target_b)
            output = model(input_var)
            loss = loss_fn(output, target_a_var) * lam + loss_fn(output, target_b_var) * (1. - lam)
        else:
            # NOTE KD Train is exclusive with mixcut, FIX it later
            # import pdb; pdb.set_trace()
            output = model(input)
            if args.KD_train:
                # teacher_model.cuda()
                teacher_outputs_tmp = []
                assert(input.shape[0]%args.teacher_step == 0)
                step_size = int(input.shape[0]//args.teacher_step)
                with torch.no_grad():
                    for k in range(0,int(input.shape[0]),step_size):
                        input_tmp = input[k:k+step_size,:,:,:]
                        teacher_outputs_tmp.append(teacher_model(input_tmp))
                        # torch.cuda.empty_cache()
                # import pdb; pdb.set_trace()
                teacher_outputs = torch.cat(teacher_outputs_tmp)
                alpha = args.KD_alpha
                T = args.KD_temperature
                loss = loss_fn(F.log_softmax(output/T, dim=1),
                                F.softmax(teacher_outputs/T, dim=1)) * (alpha * T * T) + \
                F.cross_entropy(output, target) * (1. - alpha)
            else:
                loss = loss_fn(output[0] if isinstance(output,tuple) else output, target)
        if not args.distributed:
            losses_m.update(loss.item(), input.size(0))

        optimizer.zero_grad()
        if use_amp:
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()
        optimizer.step()

        torch.cuda.synchronize()
        if model_ema is not None:
            model_ema.update(model)
        num_updates += 1

        batch_time_m.update(time.time() - end)
        if last_batch or batch_idx % args.log_interval == 0:
            lrl = [param_group['lr'] for param_group in optimizer.param_groups]
            lr = sum(lrl) / len(lrl)

            if args.distributed:
                reduced_loss = reduce_tensor(loss.data, args.world_size)
                losses_m.update(reduced_loss.item(), input.size(0))

            if args.local_rank == 0:
                logging.info(
                    'Train: {} [{:>4d}/{} ({:>3.0f}%)]  '
                    'Loss: {loss.val:>9.6f} ({loss.avg:>6.4f})  '
                    'Time: {batch_time.val:.3f}s, {rate:>7.2f}/s  '
                    '({batch_time.avg:.3f}s, {rate_avg:>7.2f}/s)  '
                    'LR: {lr:.3e}  '
                    'Data: {data_time.val:.3f} ({data_time.avg:.3f})'.format(
                        epoch,
                        batch_idx, len(loader),
                        100. * batch_idx / last_idx,
                        loss=losses_m,
                        batch_time=batch_time_m,
                        rate=input.size(0) * args.world_size / batch_time_m.val,
                        rate_avg=input.size(0) * args.world_size / batch_time_m.avg,
                        lr=lr,
                        data_time=data_time_m))

                if args.save_images and output_dir:
                    torchvision.utils.save_image(
                        input,
                        os.path.join(output_dir, 'train-batch-%d.jpg' % batch_idx),
                        padding=0,
                        normalize=True)

        if saver is not None and args.recovery_interval and (
                last_batch or (batch_idx + 1) % args.recovery_interval == 0):
            save_epoch = epoch + 1 if last_batch else epoch
            saver.save_recovery(
                model, optimizer, args, save_epoch, model_ema=model_ema, batch_idx=batch_idx)

        if lr_scheduler is not None:
            lr_scheduler.step_update(num_updates=num_updates, metric=losses_m.avg)

        end = time.time()

    return OrderedDict([('loss', losses_m.avg)])


def validate(model, loader, loss_fn, args, log_suffix=''):
    batch_time_m = AverageMeter()
    losses_m = AverageMeter()
    prec1_m = AverageMeter()
    prec5_m = AverageMeter()

    model.eval()
    model.cuda()

    end = time.time()
    last_idx = len(loader) - 1
    start = torch.cuda.Event(enable_timing=True)
    end = torch.cuda.Event(enable_timing=True)
    with torch.no_grad():
        for batch_idx, (input, target) in enumerate(loader):
            last_batch = batch_idx == last_idx
            if not args.prefetcher:
                input = input.cuda()
                target = target.cuda()

            #start.record()
            # model.cpu()
            # input.cpu()
            # target.cpu()
            end = time.time()
            output = model(input)
            batch_time_m.update(time.time() - end)
            #end.record()
            #torch.cuda.synchronize()
            #batch_time_m.update(start.elapsed_time(end))
            if isinstance(output, (tuple, list)):
                output = output[0]

            # augmentation reduction
            reduce_factor = args.tta
            if reduce_factor > 1:
                output = output.unfold(0, reduce_factor, reduce_factor).mean(dim=2)
                target = target[0:target.size(0):reduce_factor]

            loss = loss_fn(output[0] if isinstance(output,tuple) else output, target)
            prec1, prec5 = accuracy(output[0] if isinstance(output,tuple) else output, target, topk=(1, 5))
            # loss = 0
            # prec1, prec5 = 0,0

            if args.distributed:
                reduced_loss = reduce_tensor(loss.data, args.world_size)
                prec1 = reduce_tensor(prec1, args.world_size)
                prec5 = reduce_tensor(prec5, args.world_size)
            else:
                reduced_loss = loss.data
                # reduced_loss = 0

            torch.cuda.synchronize()

            losses_m.update(reduced_loss.item(), input.size(0))
            prec1_m.update(prec1.item(), output.size(0))
            prec5_m.update(prec5.item(), output.size(0))

            batch_time_m.update(time.time() - end)
            end = time.time()
            if args.local_rank == 0 and (last_batch or batch_idx % args.log_interval == 0):
                log_name = 'Test' + log_suffix
                logging.info(
                    '{0}: [{1:>4d}/{2}]  '
                    'Time: {batch_time.val:.3f} ({batch_time.avg:.3f})  '
                    'Loss: {loss.val:>7.4f} ({loss.avg:>6.4f})  '
                    'Prec@1: {top1.val:>7.4f} ({top1.avg:>7.4f})  '
                    'Prec@5: {top5.val:>7.4f} ({top5.avg:>7.4f})'.format(
                        log_name, batch_idx, last_idx,
                        batch_time=batch_time_m, loss=losses_m,
                        top1=prec1_m, top5=prec5_m))

    metrics = OrderedDict([('loss', losses_m.avg), ('prec1', prec1_m.avg), ('prec5', prec5_m.avg)])

    return metrics


if __name__ == '__main__':
    main()
