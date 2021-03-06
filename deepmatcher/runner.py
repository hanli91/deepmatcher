import logging
import math
import os
import pdb
import sys
import time
import warnings
from collections import OrderedDict

import pandas as pd

import pyprind
import torch
from tqdm import tqdm

from .data import MatchingIterator
from .optim import Optimizer, SoftNLLLoss

try:
    get_ipython
    from tqdm import tqdm_notebook as tqdm
except NameError:
    from tqdm import tqdm

logger = logging.getLogger(__name__)


class Statistics(object):
    """
    Accumulator for loss statistics, inspired by ONMT.
    Currently calculates:
    * F1
    * Precision
    * Recall
    * Accuracy
    """

    def __init__(self):
        self.loss_sum = 0
        self.examples = 0
        self.tps = 0
        self.tns = 0
        self.fps = 0
        self.fns = 0
        self.start_time = time.time()

    def update(self, loss=0, tps=0, tns=0, fps=0, fns=0):
        examples = tps + tns + fps + fns
        self.loss_sum += loss * examples
        self.tps += tps
        self.tns += tns
        self.fps += fps
        self.fns += fns
        self.examples += examples

    def loss(self):
        return self.loss_sum / self.examples

    def f1(self):
        prec = self.precision()
        recall = self.recall()
        return 2 * prec * recall / max(prec + recall, 1)

    def precision(self):
        return 100 * self.tps / max(self.tps + self.fps, 1)

    def recall(self):
        return 100 * self.tps / max(self.tps + self.fns, 1)

    def accuracy(self):
        return 100 * (self.tps + self.tns) / self.examples

    def examples_per_sec(self):
        return self.examples / (time.time() - self.start_time)


class Runner(object):
    """
    """

    @staticmethod
    def print_stats(name, epoch, batch, n_batches, stats, cum_stats):
        """Write out statistics to stdout.
        """
        print((' | {name} | [{epoch}][{batch:4d}/{n_batches}] || Loss: {loss:7.4f} |'
               ' F1: {f1:7.2f} | Prec: {prec:7.2f} | Rec: {rec:7.2f} ||'
               ' Cum. F1: {cf1:7.2f} | Cum. Prec: {cprec:7.2f} | Cum. Rec: {crec:7.2f} ||'
               ' Ex/s: {eps:6.1f}').format(
                   name=name,
                   epoch=epoch,
                   batch=batch,
                   n_batches=n_batches,
                   loss=stats.loss(),
                   f1=stats.f1(),
                   prec=stats.precision(),
                   rec=stats.recall(),
                   cf1=cum_stats.f1(),
                   cprec=cum_stats.precision(),
                   crec=cum_stats.recall(),
                   eps=cum_stats.examples_per_sec()))

    @staticmethod
    def print_final_stats(epoch, runtime, datatime, stats):
        """Write out statistics to stdout.
        """
        print(('Finished Epoch {epoch} || Run Time: {runtime:6.1f} | '
               'Load Time: {datatime:6.1f} || F1: {f1:6.2f} | Prec: {prec:6.2f} | '
               'Rec: {rec:6.2f} || Ex/s: {eps:6.2f}\n').format(
                   epoch=epoch,
                   runtime=runtime,
                   datatime=datatime,
                   f1=stats.f1(),
                   prec=stats.precision(),
                   rec=stats.recall(),
                   eps=stats.examples_per_sec()))

    @staticmethod
    def set_pbar_status(pbar, stats, cum_stats):
        postfix_dict = OrderedDict([
            ('Loss', '{0:7.4f}'.format(stats.loss())),
            ('F1', '{0:7.2f}'.format(stats.f1())),
            ('Cum. F1', '{0:7.2f}'.format(cum_stats.f1())),
            ('Ex/s', '{0:6.1f}'.format(cum_stats.examples_per_sec())),
        ])
        pbar.set_postfix(ordered_dict=postfix_dict)

    @staticmethod
    def compute_scores(output, target):
        predictions = output.max(1)[1].data
        correct = (predictions == target.data).float()
        incorrect = (1 - correct).float()
        positives = (target.data == 1).float()
        negatives = (target.data == 0).float()

        tp = torch.dot(correct, positives)
        tn = torch.dot(correct, negatives)
        fp = torch.dot(incorrect, negatives)
        fn = torch.dot(incorrect, positives)

        return tp, tn, fp, fn

    @staticmethod
    def tally_parameters(model):
        n_params = sum([p.nelement() for p in model.parameters() if p.requires_grad])
        print('* Number of trainable parameters:', n_params)

    @staticmethod
    def _run(run_type,
             model,
             dataset,
             criterion=None,
             optimizer=None,
             train=False,
             device=None,
             save_path=None,
             batch_size=32,
             num_data_workers=2,
             batch_callback=None,
             epoch_callback=None,
             progress_style='bar',
             log_freq=5,
             sort_in_buckets=None,
             return_predictions=False,
             **kwargs):

        sort_in_buckets = train
        run_iter = MatchingIterator(
            dataset,
            model.train_dataset,
            batch_size=batch_size,
            device=device,
            sort_in_buckets=sort_in_buckets)

        if device == 'cpu':
            model = model.cpu()
            if criterion:
                criterion = criterion.cpu()
        elif torch.cuda.is_available():
            model = model.cuda()
            if criterion:
                criterion = criterion.cuda()
        elif device == 'gpu':
            raise ValueError('No GPU available.')

        if train:
            model.train()
        else:
            model.eval()

        # Init model
        init_batch = next(run_iter.__iter__())
        model(init_batch)

        epoch = model.epoch
        datatime = 0
        runtime = 0
        cum_stats = Statistics()
        stats = Statistics()
        predictions = []
        id_attr = model.train_dataset.id_field
        label_attr = model.train_dataset.label_field

        if train and epoch == 0:
            Runner.tally_parameters(model)

        epoch_str = 'Epoch ' + str(epoch + 1) + ' :'
        print('===> ', run_type, epoch_str)
        batch_end = time.time()

        # The tqdm-bar for Jupyter notebook is under development.
        if progress_style == 'tqdm-bar':
            pbar = tqdm(
                total=len(run_iter) // log_freq,
                bar_format='{l_bar}{bar}{postfix}',
                file=sys.stdout)

        # Use the pyprind bar as the default progress bar.
        if progress_style == 'bar':
            pbar = pyprind.ProgBar(len(run_iter) // log_freq, bar_char='█', width=30)

        for batch_idx, batch in enumerate(run_iter):
            batch_start = time.time()
            datatime += batch_start - batch_end

            output = model(batch)

            # from torchviz import make_dot, make_dot_from_trace
            # dot = make_dot(output.mean(), params=dict(model.named_parameters()))
            # pdb.set_trace()

            loss = float('NaN')
            if criterion:
                loss = criterion(output, getattr(batch, label_attr))

            if hasattr(batch, label_attr):
                scores = Runner.compute_scores(output, getattr(batch, label_attr))
            else:
                scores = [0] * 4

            cum_stats.update(float(loss), *scores)
            stats.update(float(loss), *scores)

            if return_predictions:
                predicted = output.max(1)[1].data
                for idx, id in enumerate(getattr(batch, id_attr)):
                    predictions.append((id, float(output[idx, 1].exp())))

            if (batch_idx + 1) % log_freq == 0:
                if progress_style == 'log':
                    Runner.print_stats(run_type, epoch + 1, batch_idx + 1, len(run_iter),
                                       stats, cum_stats)
                elif progress_style == 'tqdm-bar':
                    pbar.update()
                    Runner.set_pbar_status(pbar, stats, cum_stats)
                elif progress_style == 'bar':
                    pbar.update()
                stats = Statistics()

            if train:
                model.zero_grad()
                loss.backward()

                if not optimizer.params:
                    optimizer.set_parameters(model.named_parameters())
                optimizer.step()

            batch_end = time.time()
            runtime += batch_end - batch_start

        if progress_style == 'tqdm-bar':
            pbar.close()
        elif progress_style == 'bar':
            sys.stderr.flush()

        Runner.print_final_stats(epoch + 1, runtime, datatime, cum_stats)

        if return_predictions:
            return predictions
        else:
            return cum_stats.f1()

    @staticmethod
    def train(model,
              train_dataset,
              validation_dataset,
              best_save_path,
              epochs=30,
              criterion=None,
              optimizer=None,
              pos_neg_ratio=None,
              pos_weight=None,
              label_smoothing=0.05,
              save_every_prefix=None,
              save_every_freq=None,
              **kwargs):
        model.initialize(train_dataset)

        model.register_train_buffer('optimizer_state')
        model.register_train_buffer('best_score')
        model.register_train_buffer('epoch')

        if criterion is None:
            if pos_weight is not None:
                assert pos_weight < 2
                warnings.warn("'pos_weight' is deprecated and will be removed in a later "
                              "release, please use 'pos_neg_ratio' instead",
                              DeprecationWarning)
                assert pos_neg_ratio is None
            else:
                if pos_neg_ratio is None:
                    pos_neg_ratio = 1
                else:
                    assert pos_neg_ratio > 0
                pos_weight = 2 * pos_neg_ratio / (1 + pos_neg_ratio)

            neg_weight = 2 - pos_weight

            criterion = SoftNLLLoss(label_smoothing,
                                    torch.Tensor([neg_weight, pos_weight]))

        optimizer = optimizer or Optimizer()
        if model.optimizer_state is not None:
            model.optimizer.base_optimizer.load_state_dict(model.optimizer_state)

        if model.epoch is None:
            epochs_range = range(epochs)
        else:
            epochs_range = range(model.epoch + 1, epochs)

        if model.best_score is None:
            model.best_score = -1
        optimizer.last_acc = model.best_score

        for epoch in epochs_range:
            model.epoch = epoch
            Runner._run(
                'TRAIN', model, train_dataset, criterion, optimizer, train=True, **kwargs)

            score = Runner._run('EVAL', model, validation_dataset, train=False, **kwargs)

            optimizer.update_learning_rate(score, epoch)
            model.optimizer_state = optimizer.base_optimizer.state_dict()

            new_best_found = False
            if score > model.best_score:
                print('* Best F1:', score)
                model.best_score = score
                new_best_found = True

                if best_save_path and new_best_found:
                    print('Saving best model...')
                    model.save_state(best_save_path)

            if save_every_prefix is not None and (epoch + 1) % save_every_freq == 0:
                save_path = '{prefix}_ep{epoch}.pth'.format(
                    prefix=save_every_prefix, epoch=epoch)
                model.save_state(save_path)

        print('Loading best model...')
        model.load_state(best_save_path)

    def eval(model, dataset, **kwargs):
        return Runner._run('EVAL', model, dataset, **kwargs)

    def predict(model, dataset, output_attributes=False, **kwargs):
        predictions = Runner._run(
            'PREDICT', model, dataset, return_predictions=True, **kwargs)
        pred_table = pd.DataFrame(predictions, columns=(dataset.id_field, 'match_score'))
        pred_table = pred_table.set_index(dataset.id_field)

        if output_attributes:
            raw_table = pd.read_csv(dataset.path).set_index(dataset.id_field)
            raw_table.index = raw_table.index.astype('str')
            pred_table = pred_table.join(raw_table)

        return pred_table
