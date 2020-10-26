import argparse
import os
import random
import shutil
import time
import warnings

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim
import torch.multiprocessing as mp
import torch.utils.data
import torch.utils.data.distributed
import torchvision.transforms as transforms

import train_util
import pdb
import matplotlib.pyplot as plt
from model import UNet
import numpy as np
from torch.utils.tensorboard import SummaryWriter

from sklearn.metrics import precision_recall_curve
from sklearn.metrics import classification_report

parser = argparse.ArgumentParser(description='Interaction Pre-Training')

parser.add_argument('-j', '--workers', default=4, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('-b', '--batch-size', default=128, type=int,
                    metavar='N')
parser.add_argument('--lr', '--learning-rate', default=0.01, type=float,
                    metavar='LR', help='initial learning rate', dest='lr')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--wd', '--weight-decay', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)',
                    dest='weight_decay')
parser.add_argument('-p', '--print-freq', default=100, type=int,
                    metavar='N', help='print frequency (default: 100)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
parser.add_argument('--use-depth', dest='use_depth', action='store_true',
                    help='use depth as input')
# parser.add_argument('--seed', default=None, type=int,
                    # help='seed for initializing training. ')
parser.add_argument('--gpu', default=None, type=int,
                    help='GPU id to use.')

best_acc1 = 0

def main():
    args = parser.parse_args()
    writer = SummaryWriter('runs/{}'.format(
                           'rgbd' if args.use_depth else 'rgb'))

    # if args.seed is not None:
        # random.seed(args.seed)
        # torch.manual_seed(args.seed)
        # cudnn.deterministic = True
        # warnings.warn('You have chosen to seed training. '
                      # 'This will turn on the CUDNN deterministic setting, '
                      # 'which can slow down your training considerably! '
                      # 'You may see unexpected behavior when restarting '
                      # 'from checkpoints.')

    ngpus_per_node = torch.cuda.device_count()
    main_worker(args, writer)


def main_worker(args, writer):
    global best_acc1

    # create model
    model = UNet(input_channels=4 if args.use_depth else 3)
    model = torch.nn.DataParallel(model).cuda()

    # define loss function (criterion) and optimizer
    weight=torch.from_numpy(np.array([1.,30.]).astype(np.float32))
    criterion = nn.CrossEntropyLoss(weight=weight).cuda()

    optimizer = torch.optim.SGD(model.parameters(), args.lr,
                                momentum=args.momentum,
                                weight_decay=args.weight_decay)

    experiment_name =  'rgbd' if args.use_depth else 'rgb'
    save_dir = './ckpt/{}'.format(experiment_name)
    os.makedirs(save_dir, exist_ok=True)

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            print("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch']
            best_acc1 = checkpoint['best_acc1']
            model.load_state_dict(checkpoint['state_dict'])
            optimizer.load_state_dict(checkpoint['optimizer'])
            print("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            print("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    # Data loading code
    train_dataset = train_util.iGibsonInteractionPretrain(
                                load_depth=args.use_depth,
                                train=True)
    train_loader = torch.utils.data.DataLoader(
        train_dataset, batch_size=args.batch_size, shuffle=True,
        num_workers=args.workers, pin_memory=True)

    val_dataset= train_util.iGibsonInteractionPretrain(
                                load_depth=args.use_depth,
                                train=False)
    val_loader = torch.utils.data.DataLoader(
        val_dataset, batch_size=args.batch_size, shuffle=False,
        num_workers=args.workers, pin_memory=True)
    
    if args.evaluate:
        validate(val_loader, model, criterion, args,
                os.path.join(save_dir, '{:04d}'.format(args.start_epoch - 1)), 
                writer, args.start_epoch)
        return

    for epoch in range(args.start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch, args)

        # train for one epoch
        train(train_loader, model, criterion, optimizer, 
              epoch, args, writer)

        # evaluate on validation set
        acc1 = validate(val_loader, model, criterion, args, 
                        os.path.join(save_dir, '{:04d}'.format(epoch)),
                        writer, epoch)

        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)

        save_checkpoint( save_dir, {
            'epoch': epoch + 1,
            'state_dict': model.state_dict(),
            'best_acc1': best_acc1,
            'optimizer' : optimizer.state_dict(),
            }, is_best, 'ckpt_{:04d}.pth.tar'.format(epoch))


def train(train_loader, 
          model,
          criterion, optimizer, 
          epoch, args, writer):
    batch_time = AverageMeter('Time', ':6.3f')
    data_time = AverageMeter('Data', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    progress = ProgressMeter(
        len(train_loader),
        [batch_time, data_time, losses, top1],
        prefix="Epoch: [{}]".format(epoch))

    # switch to train mode
    model.train()

    end = time.time()
    for i, sample in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        images = sample['image'].cuda(non_blocking=True)
        target = sample['label'].cuda(non_blocking=True)
        action = sample['action'].cuda(non_blocking=True)

        # compute output
        pred, _ = model(images)
        pred_flat = torch.flatten(pred, 2, -1)
        eI = action[..., None, None].expand(pred_flat.size(0), 2, 1)
        Y = torch.gather(pred_flat, dim=2, index=eI).squeeze()

        loss = criterion(Y, target)

        # measure accuracy and record loss
        acc1 = accuracy(Y, target)[0].cpu().numpy()[0]
        losses.update(loss.item(), images.size(0))
        top1.update(acc1, images.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % args.print_freq == 0:
            progress.display(i)
            # ...log the running loss
            writer.add_scalar('training loss',
                            loss.item(),
                            epoch * len(train_loader) + i)
            writer.add_scalar('training accuracy',
                            acc1,
                            epoch * len(train_loader) + i)

def validate(val_loader, model, criterion, args, viz_dir, writer, epoch):
    os.makedirs(viz_dir, exist_ok=True)
    batch_time = AverageMeter('Time', ':6.3f')
    losses = AverageMeter('Loss', ':.4e')
    top1 = AverageMeter('Acc@1', ':6.2f')
    progress = ProgressMeter(
        len(val_loader),
        [batch_time, losses, top1],
        prefix='Test: ')

    # switch to evaluate mode
    model.eval()

    with torch.no_grad():
        agg_predicted = []
        agg_label = []
        end = time.time()
        softmax = nn.Softmax(dim=1)
        for i, sample in enumerate(val_loader):

            images = sample['image'].cuda(non_blocking=True)
            target = sample['label'].cuda(non_blocking=True)
            action = sample['action'].cuda(non_blocking=True)

            # compute output
            pred, features = model(images)
            pred_flat = torch.flatten(pred, 2, -1)
            eI = action[..., None, None].expand(pred_flat.size(0), 2, 1)
            Y = torch.gather(pred_flat, dim=2, index=eI).squeeze()

            loss = criterion(Y, target)

            # measure accuracy and record loss
            acc1 = accuracy(Y, target)[0].cpu().numpy()[0]
            losses.update(loss.item(), images.size(0))
            top1.update(acc1, images.size(0))

            # store pred and label, for later precision/recall/f1
            y_prob = softmax(Y.cpu()).numpy()[:,1]
            agg_predicted.append(y_prob)
            agg_label.append(target.cpu().numpy())

            # measure elapsed time
            batch_time.update(time.time() - end)
            end = time.time()

            if args.print_freq != 0 and  i % args.print_freq == 0:
                progress.display(i)
                train_util.visualize_data_entry(sample,features,
                        Y,pred,i, save_path=viz_dir, save_first=False)

                # ...log the running loss
                writer.add_scalar('validation loss',
                                loss.item(),
                                epoch * len(val_loader) + i)
                writer.add_scalar('validation accuracy',
                                acc1,
                                epoch * len(val_loader) + i)

                # visualize mini-batch
                writer.add_figure('predictions vs. actuals',
                                train_util.visualize_data_entry(
                                    sample,features,
                                    Y,pred,i,return_figure=True),
                                epoch * len(val_loader) + i)

        # process precision/recall/f1
        all_pred = np.concatenate(agg_predicted)
        all_label = np.concatenate(agg_label)
        
        # process precision recall curve
        fig = get_precision_recall_curve(all_label, all_pred, epoch)
        fig.savefig(os.path.join(viz_dir, 'prec_recall.png'))
        writer.add_figure('precision vs recall',
                        fig, epoch * len(val_loader))
        fig.clf()
        plt.close()

        # get values
        values = classification_report(all_label, all_pred > 0.5, output_dict=True)['1']
        writer.add_scalar('val precision',
                        values['precision'],
                        epoch * len(val_loader))
        writer.add_scalar('val recall',
                        values['recall'],
                        epoch * len(val_loader))
        writer.add_scalar('val f1',
                        values['f1-score'],
                        epoch * len(val_loader))

        # TODO: this should also be done with the ProgressMeter
        print(' * Acc@1 {top1.avg:.3f}'
              .format(top1=top1))
    return top1.avg

def get_precision_recall_curve(all_label, all_pred, epoch):
    precision_steps, recall_steps, _ = precision_recall_curve(all_label, all_pred)
    fig,ax = plt.subplots(nrows=1,ncols=1,figsize=(8,8))
    # plot the precision-recall curves
    ax.plot(recall_steps, precision_steps, label='epoch {}'.format(epoch))
    ax.set_xlabel('Recall')
    ax.set_ylabel('Precision')
    ax.set_title('Precision-Recall curve')
    ax.legend()
    return fig

def save_checkpoint(save_dir, state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, os.path.join(save_dir, filename))
    if is_best:
        shutil.copyfile(os.path.join(save_dir, filename), 'model_best.pth.tar')


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self, name, fmt=':f'):
        self.name = name
        self.fmt = fmt
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count

    def __str__(self):
        fmtstr = '{name} {val' + self.fmt + '} ({avg' + self.fmt + '})'
        return fmtstr.format(**self.__dict__)


class ProgressMeter(object):
    def __init__(self, num_batches, meters, prefix=""):
        self.batch_fmtstr = self._get_batch_fmtstr(num_batches)
        self.meters = meters
        self.prefix = prefix

    def display(self, batch):
        entries = [self.prefix + self.batch_fmtstr.format(batch)]
        entries += [str(meter) for meter in self.meters]
        print('\t'.join(entries))

    def _get_batch_fmtstr(self, num_batches):
        num_digits = len(str(num_batches // 1))
        fmt = '{:' + str(num_digits) + 'd}'
        return '[' + fmt + '/' + fmt.format(num_batches) + ']'


def adjust_learning_rate(optimizer, epoch, args):
    """Sets the learning rate to the initial LR decayed by 10 every 30 epochs"""
    lr = args.lr * (0.1 ** (epoch // 30))
    for param_group in optimizer.param_groups:
        param_group['lr'] = lr


def accuracy(output, target, topk=(1,)):
    """Computes the accuracy over the k top predictions for the specified values of k"""
    with torch.no_grad():
        maxk = max(topk)
        batch_size = target.size(0)

        _, pred = output.topk(maxk, 1, True, True)
        pred = pred.t()
        correct = pred.eq(target.view(1, -1).expand_as(pred))

        res = []
        for k in topk:
            correct_k = correct[:k].reshape(-1).float().sum(0, keepdim=True)
            res.append(correct_k.mul_(100.0 / batch_size))
        return res


if __name__ == '__main__':
    main()
