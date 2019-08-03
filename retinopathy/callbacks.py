import itertools
import os
from functools import partial
from typing import List, Dict

import numpy as np
import pandas as pd
import torch
import torch.nn.functional as F
from catalyst.contrib.registry import GRAD_CLIPPERS
from catalyst.dl import MetricCallback, RunnerState, Callback, CriterionCallback, OptimizerCallback
from catalyst.dl.callbacks import MixupCallback
from catalyst.utils import get_optimizer_momentum
from pytorch_toolbelt.utils.catalyst import get_tensorboard_logger
from pytorch_toolbelt.utils.torch_utils import to_numpy
from pytorch_toolbelt.utils.visualization import plot_confusion_matrix, render_figure_to_tensor
from sklearn.metrics import confusion_matrix
from torch import nn
from torch.nn import Module, Parameter

from retinopathy.models.ordinal import LogisticCumulativeLink
from retinopathy.models.regression import regression_to_class
from retinopathy.rounder import OptimizedRounder


def cohen_kappa_score(y1, y2, labels=None, weights=None, sample_weight=None):
    r"""Cohen's kappa: a statistic that measures inter-annotator agreement.

    This function computes Cohen's kappa [1]_, a score that expresses the level
    of agreement between two annotators on a classification problem. It is
    defined as

    .. math::
        \kappa = (p_o - p_e) / (1 - p_e)

    where :math:`p_o` is the empirical probability of agreement on the label
    assigned to any sample (the observed agreement ratio), and :math:`p_e` is
    the expected agreement when both annotators assign labels randomly.
    :math:`p_e` is estimated using a per-annotator empirical prior over the
    class labels [2]_.

    Read more in the :ref:`User Guide <cohen_kappa>`.

    Parameters
    ----------
    y1 : array, shape = [n_samples]
        Labels assigned by the first annotator.

    y2 : array, shape = [n_samples]
        Labels assigned by the second annotator. The kappa statistic is
        symmetric, so swapping ``y1`` and ``y2`` doesn't change the value.

    labels : array, shape = [n_classes], optional
        List of labels to index the matrix. This may be used to select a
        subset of labels. If None, all labels that appear at least once in
        ``y1`` or ``y2`` are used.

    weights : str, optional
        List of weighting type to calculate the score. None means no weighted;
        "linear" means linear weighted; "quadratic" means quadratic weighted.

    sample_weight : array-like of shape = [n_samples], optional
        Sample weights.

    Returns
    -------
    kappa : float
        The kappa statistic, which is a number between -1 and 1. The maximum
        value means complete agreement; zero or lower means chance agreement.

    References
    ----------
    .. [1] J. Cohen (1960). "A coefficient of agreement for nominal scales".
           Educational and Psychological Measurement 20(1):37-46.
           doi:10.1177/001316446002000104.
    .. [2] `R. Artstein and M. Poesio (2008). "Inter-coder agreement for
           computational linguistics". Computational Linguistics 34(4):555-596.
           <https://www.mitpressjournals.org/doi/pdf/10.1162/coli.07-034-R2>`_
    .. [3] `Wikipedia entry for the Cohen's kappa.
            <https://en.wikipedia.org/wiki/Cohen%27s_kappa>`_
    """
    confusion = confusion_matrix(y1, y2, labels=labels,
                                 sample_weight=sample_weight)
    n_classes = confusion.shape[0]
    sum0 = np.sum(confusion, axis=0)
    sum1 = np.sum(confusion, axis=1)
    expected = np.outer(sum0, sum1) / np.sum(sum0)

    if weights is None:
        w_mat = np.ones([n_classes, n_classes], dtype=np.int)
        w_mat.flat[:: n_classes + 1] = 0
    elif weights == "linear" or weights == "quadratic":
        w_mat = np.zeros([n_classes, n_classes], dtype=np.int)
        w_mat += np.arange(n_classes)
        if weights == "linear":
            w_mat = np.abs(w_mat - w_mat.T)
        else:
            w_mat = (w_mat - w_mat.T) ** 2
    else:
        raise ValueError("Unknown kappa weighting type.")

    num = w_mat * confusion
    denom = w_mat * expected
    k = np.sum(num) / np.sum(denom)
    return 1 - k, num, denom


def plot_matrix(cm, class_names,
                figsize=(16, 16),
                title='Matrix',
                fname=None,
                noshow=False):
    """Render the confusion matrix and return matplotlib's figure with it.
    Normalization can be applied by setting `normalize=True`.
    """
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    cmap = plt.cm.Oranges

    f = plt.figure(figsize=figsize)
    plt.title(title)
    plt.imshow(cm, interpolation='nearest', cmap=cmap)

    tick_marks = np.arange(len(class_names))
    plt.xticks(tick_marks, class_names, rotation=45, ha='right')
    # f.tick_params(direction='inout')
    # f.set_xticklabels(varLabels, rotation=45, ha='right')
    # f.set_yticklabels(varLabels, rotation=45, va='top')

    plt.yticks(tick_marks, class_names)

    fmt = '.2f'
    thresh = cm.max() / 2.
    for i, j in itertools.product(range(cm.shape[0]), range(cm.shape[1])):
        plt.text(j, i, format(cm[i, j], fmt),
                 horizontalalignment="center",
                 color="white" if cm[i, j] > thresh else "black")

    plt.tight_layout()
    plt.ylabel('True label')
    plt.xlabel('Predicted label')

    if fname is not None:
        plt.savefig(fname=fname)

    if not noshow:
        plt.show()

    return f


class CappaScoreCallback(Callback):
    def __init__(self,
                 input_key: str = "targets",
                 output_key: str = "logits",
                 prefix: str = "kappa_score",
                 from_regression=False,
                 ignore_index=-100,
                 class_names=None):
        """
        :param input_key: input key to use for precision calculation; specifies our `y_true`.
        :param output_key: output key to use for precision calculation; specifies our `y_pred`.
        """
        self.prefix = prefix
        self.output_key = output_key
        self.input_key = input_key
        self.targets = []
        self.predictions = []
        self.ignore_index = ignore_index
        self.from_regression = from_regression
        self.class_names = class_names

    def on_loader_start(self, state):
        self.targets = []
        self.predictions = []

    def on_batch_end(self, state: RunnerState):

        targets = to_numpy(state.input[self.input_key].detach())
        outputs = to_numpy(state.output[self.output_key].detach())

        if self.ignore_index is not None:
            mask = targets != self.ignore_index
            outputs = outputs[mask]
            targets = targets[mask]

        self.targets.extend(targets)
        self.predictions.extend(outputs)

    def on_loader_end(self, state):
        predictions = to_numpy(self.predictions)

        if self.from_regression:
            rounder = OptimizedRounder()
            coeff = rounder.fit(predictions, self.targets)
            optimized_predictions = rounder.predict(predictions, coeff)
            predictions = regression_to_class(predictions)

            self._log(state, self.prefix + "_opt", optimized_predictions, self.targets)
        else:
            predictions = np.argmax(predictions, axis=1)

        self._log(state, self.prefix, predictions, self.targets)

    def _log(self, state, prefix, predictions, targets):
        score, num, denom = cohen_kappa_score(predictions, targets, weights='quadratic')

        if self.class_names is None:
            class_names = [str(i) for i in range(num.shape[1])]
        else:
            class_names = self.class_names

        num_classes = len(class_names)

        num_fig = plot_matrix(num / np.sum(num),
                              figsize=(6 + num_classes // 3, 6 + num_classes // 3),
                              class_names=class_names,
                              noshow=True)

        denom_fig = plot_matrix(denom / np.sum(denom),
                                figsize=(6 + num_classes // 3, 6 + num_classes // 3),
                                class_names=class_names,
                                noshow=True)

        num_fig = render_figure_to_tensor(num_fig)
        denom_fig = render_figure_to_tensor(denom_fig)

        logger = get_tensorboard_logger(state)
        logger.add_image(f'{prefix}/epoch/num', num_fig, global_step=state.step)
        logger.add_image(f'{prefix}/epoch/denom', denom_fig, global_step=state.step)
        state.metrics.epoch_values[state.loader_name][prefix] = score


def custom_accuracy_fn(outputs, targets, from_regression=False, ignore_index=None):
    """
    Computes the accuracy@k for the specified values of k
    """
    outputs = outputs.detach()
    targets = targets.detach()

    if ignore_index is not None:
        mask = targets != ignore_index
        outputs = outputs[mask]
        targets = targets[mask]

    batch_size = targets.size(0)
    if batch_size == 0:
        return np.nan

    if from_regression:
        outputs = regression_to_class(outputs).long()
    else:
        outputs = outputs.argmax(dim=1)

    correct = outputs.eq(targets.long())

    acc = correct.float().sum() / batch_size
    return acc


class CustomAccuracyCallback(MetricCallback):
    """
    Accuracy metric callback.
    """

    def __init__(
            self,
            input_key: str = "targets",
            output_key: str = "logits",
            prefix: str = "accuracy",
            ignore_index=None,
            from_regression=False
    ):
        """
        Args:
            input_key: input key to use for accuracy calculation;
                specifies our `y_true`.
            output_key: output key to use for accuracy calculation;
                specifies our `y_pred`.
        """
        super().__init__(
            prefix=prefix,
            metric_fn=partial(custom_accuracy_fn, from_regression=from_regression, ignore_index=ignore_index),
            input_key=input_key,
            output_key=output_key
        )


class ConfusionMatrixCallbackFromRegression(Callback):
    """
    Compute and log confusion matrix to Tensorboard.
    For use with Multiclass classification/segmentation.
    """

    def __init__(
            self,
            input_key: str = "targets",
            output_key: str = "logits",
            prefix: str = "confusion_matrix",
            class_names=None,
            ignore_index=None
    ):
        """
        :param input_key: input key to use for precision calculation;
            specifies our `y_true`.
        :param output_key: output key to use for precision calculation;
            specifies our `y_pred`.
        """
        self.prefix = prefix
        self.class_names = class_names
        self.output_key = output_key
        self.input_key = input_key
        self.outputs = []
        self.targets = []
        self.ignore_index = ignore_index

    def on_loader_start(self, state):
        self.outputs = []
        self.targets = []

    def on_batch_end(self, state: RunnerState):
        outputs = to_numpy(regression_to_class(state.output[self.output_key].detach()))
        targets = to_numpy(state.input[self.input_key])

        if self.ignore_index is not None:
            mask = targets != self.ignore_index
            outputs = outputs[mask]
            targets = targets[mask]

        self.outputs.extend(outputs)
        self.targets.extend(targets)

    def on_loader_end(self, state):
        targets = np.array(self.targets)
        outputs = np.array(self.outputs)

        if self.class_names is None:
            class_names = [str(i) for i in range(targets.shape[1])]
        else:
            class_names = self.class_names

        num_classes = len(class_names)
        cm = confusion_matrix(outputs, targets, labels=range(num_classes))

        fig = plot_confusion_matrix(cm,
                                    figsize=(6 + num_classes // 3, 6 + num_classes // 3),
                                    class_names=class_names,
                                    normalize=True,
                                    noshow=True)
        fig = render_figure_to_tensor(fig)

        logger = get_tensorboard_logger(state)
        logger.add_image(f'{self.prefix}/epoch', fig, global_step=state.step)


class SWACallback(Callback):
    """
    Callback for use :'torchcontrib.optim.SWA'
    """

    def __init__(self, optimizer):
        self.optimizer = optimizer

    def on_loader_end(self, state: RunnerState):
        from torchcontrib.optim.swa import SWA
        if state.loader_name == 'train':
            self.optimizer.swap_swa_sgd()
            SWA.bn_update(state.loaders, state.model, state.device)


class MixupSameLabelCallback(CriterionCallback):
    """
    Callback to do mixup augmentation.

    Paper: https://arxiv.org/abs/1710.09412

    Note:
        MixupCallback is inherited from CriterionCallback and
        does its work.

        You may not use them together.
    """

    def __init__(
            self,
            fields: List[str] = ("features",),
            alpha=1.3,
            on_train_only=True,
            **kwargs
    ):
        """
        Args:
            fields (List[str]): list of features which must be affected.
            alpha (float): beta distribution a=b parameters.
                Must be >=0. The more alpha closer to zero
                the less effect of the mixup.
            on_train_only (bool): Apply to train only.
                As the mixup use the proxy inputs, the targets are also proxy.
                We are not interested in them, are we?
                So, if on_train_only is True, use a standard output/metric
                for validation.
        """
        assert len(fields) > 0, \
            "At least one field for MixupCallback is required"
        assert alpha >= 0, "alpha must be>=0"

        super().__init__(**kwargs)

        self.on_train_only = on_train_only
        self.fields = fields
        self.alpha = alpha
        self.target_key = 'targets'
        self.lam = 1
        self.index = None
        self.is_needed = True

    def on_loader_start(self, state: RunnerState):
        self.is_needed = not self.on_train_only or \
                         state.loader_name.startswith("train")

    def on_batch_start(self, state: RunnerState):
        if not self.is_needed:
            return

        targets = state.input[self.target_key]

        for label_index in torch.arange(5):
            mask = targets == label_index
            lam = np.random.beta(self.alpha, self.alpha)

            index = torch.randperm(mask.shape[0])

            for f in self.fields:
                state.input[f][mask] = lam * state.input[f][mask] + \
                                       (1 - lam) * state.input[f][mask][index]

    def _compute_loss(self, state: RunnerState, criterion):
        # As we don't change target, compute basic loss
        return super()._compute_loss(state, criterion)


class MixupRegressionCallback(MixupCallback):
    """
    Callback to do mixup augmentation.
    It's modification compute recompute the target according to:
    ```
        y = max(y_a, y_b)
    ```
    Paper: https://arxiv.org/abs/1710.09412

    Note:
        MixupCallback is inherited from CriterionCallback and
        does its work.

        You may not use them together.
    """

    def __init__(self, fields: List[str] = ("features",), alpha=1.5, on_train_only=True, **kwargs):
        """
        Note we set alpha 1.5 to enforce mixing
        :param fields:
        :param alpha:
        :param on_train_only:
        :param kwargs:
        """
        super().__init__(fields, alpha, on_train_only, **kwargs)

    def on_batch_start(self, state: RunnerState):
        if not self.is_needed:
            return

        if self.alpha > 0:
            self.lam = np.random.beta(self.alpha, self.alpha)
        else:
            self.lam = 1

        if self.lam < 0.3 or self.lam > 0.7:
            # Do not apply mixup on small lambdas
            return

        self.index = torch.randperm(state.input[self.fields[0]].shape[0])
        self.index.to(state.device)

        for f in self.fields:
            state.input[f] = self.lam * state.input[f] + \
                             (1 - self.lam) * state.input[f][self.index]

    def _compute_loss(self, state: RunnerState, criterion):
        if not self.is_needed:
            return super()._compute_loss(state, criterion)

        if self.lam < 0.3 or self.lam > 0.7:
            # Do not apply mixup on small lambdas
            return super()._compute_loss(state, criterion)

        pred = state.output[self.output_key]
        y_a: torch.Tensor = state.input[self.input_key]
        y_b: torch.Tensor = state.input[self.input_key][self.index]
        # y = max(y_a, y_b)

        # In case of regression, if we do mixup of images of DR of different stages,
        # we assign the maximum stage as our target
        mask = y_b > y_a
        y = y_a.masked_scatter(mask, y_b[mask])

        loss = criterion(pred, y)
        return loss


class UnsupervisedCriterionCallback(CriterionCallback):
    """
    """

    def __init__(
            self,
            input_key='original',
            output_key='logits',
            target_key='targets',
            on_train_only=True,
            unsupervised_label=-100,
            **kwargs
    ):
        """
        Args:
            fields (List[str]): list of features which must be affected.
            alpha (float): beta distribution a=b parameters.
                Must be >=0. The more alpha closer to zero
                the less effect of the mixup.
            on_train_only (bool): Apply to train only.
                As the mixup use the proxy inputs, the targets are also proxy.
                We are not interested in them, are we?
                So, if on_train_only is True, use a standard output/metric
                for validation.
        """
        super().__init__(**kwargs)

        self.on_train_only = on_train_only
        self.input_key = input_key
        self.target_key = target_key
        self.output_key = output_key
        self.is_needed = True
        self.unsupervised_label = unsupervised_label

    def on_loader_start(self, state: RunnerState):
        self.is_needed = not self.on_train_only or \
                         state.loader_name.startswith("train")

    def on_batch_end(self, state: RunnerState):
        targets = state.input[self.target_key]
        mask = targets == self.unsupervised_label

        if not mask.any() or not self.is_needed:
            # If batch contains no unsupervised samples - quit
            state.metrics.add_batch_value(metrics_dict={
                self.prefix: 0,
            })

            return

        non_augmented_image: torch.Tensor = state.input[self.input_key]

        # Compute target probability distribution
        training = state.model.training
        state.model.eval()
        non_augmented_logits = state.model(non_augmented_image)[self.output_key][mask]
        state.model.train(training)

        non_augmented_probs: torch.Tensor = F.log_softmax(non_augmented_logits, dim=1).exp()

        augmented_logits = state.output[self.output_key][mask]
        augmented_log_prob = F.log_softmax(augmented_logits, dim=1)

        loss = F.kl_div(augmented_log_prob, non_augmented_probs, reduction='batchmean')

        state.metrics.add_batch_value(metrics_dict={
            self.prefix: loss.item(),
        })

        self._add_loss_to_state(state, loss)


class L2RegularizationCallback(CriterionCallback):
    """
    """

    def __init__(
            self,
            on_train_only=True,
            apply_to_bias=False,
            multiplier=1e-4,
            prefix: str = "l2_regularization",
            loss_key=None,
            **kwargs
    ):
        """
        Args:
            fields (List[str]): list of features which must be affected.
            alpha (float): beta distribution a=b parameters.
                Must be >=0. The more alpha closer to zero
                the less effect of the mixup.
            on_train_only (bool): Apply to train only.
                As the mixup use the proxy inputs, the targets are also proxy.
                We are not interested in them, are we?
                So, if on_train_only is True, use a standard output/metric
                for validation.
        """
        super().__init__(prefix=prefix, multiplier=multiplier, loss_key=loss_key)

        self.on_train_only = on_train_only
        self.is_needed = True
        self.apply_to_bias = apply_to_bias

    def on_loader_start(self, state: RunnerState):
        self.is_needed = not self.on_train_only or \
                         state.loader_name.startswith("train")

    def on_batch_end(self, state: RunnerState):
        l2_reg = 0

        for param_name, param in state.model.named_parameters():
            if param_name.endswith('bias') and not self.apply_to_bias:
                continue

            if param.requires_grad:
                l2_reg = param.norm(2) * self.multiplier + l2_reg

        state.metrics.add_batch_value(metrics_dict={
            self.prefix: l2_reg.item(),
        })
        self._add_loss_to_state(state, l2_reg)


class L1RegularizationCallback(CriterionCallback):
    """
    """

    def __init__(
            self,
            on_train_only=True,
            apply_to_bias=False,
            multiplier=1e-4,
            prefix: str = "l1_regularization",
            loss_key=None,
            **kwargs
    ):
        """
        Args:
            fields (List[str]): list of features which must be affected.
            alpha (float): beta distribution a=b parameters.
                Must be >=0. The more alpha closer to zero
                the less effect of the mixup.
            on_train_only (bool): Apply to train only.
                As the mixup use the proxy inputs, the targets are also proxy.
                We are not interested in them, are we?
                So, if on_train_only is True, use a standard output/metric
                for validation.
        """
        super().__init__(prefix=prefix, multiplier=multiplier, loss_key=loss_key)

        self.on_train_only = on_train_only
        self.is_needed = True
        self.apply_to_bias = apply_to_bias

    def on_loader_start(self, state: RunnerState):
        self.is_needed = not self.on_train_only or \
                         state.loader_name.startswith("train")

    def on_batch_end(self, state: RunnerState):
        l1_reg = 0

        for param_name, param in state.model.named_parameters():
            if param_name.endswith('bias') and not self.apply_to_bias:
                continue

            if param.requires_grad:
                l1_reg = param.norm(1) * self.multiplier + l1_reg

        state.metrics.add_batch_value(metrics_dict={
            self.prefix: l1_reg.item(),
        })
        self._add_loss_to_state(state, l1_reg)


class AscensionCallback(Callback):
    """
    Ensure that each cutpoint is ordered in ascending value.
    e.g.
    .. < cutpoint[i - 1] < cutpoint[i] < cutpoint[i + 1] < ...
    This is done by clipping the cutpoint values at the end of a batch gradient
    update. By no means is this an efficient way to do things, but it works out
    of the box with stochastic gradient descent.
    Parameters
    ----------
    margin : float, (default=0.0)
        The minimum value between any two adjacent cutpoints.
        e.g. enforce that cutpoint[i - 1] + margin < cutpoint[i]
    min_val : float, (default=-1e6)
        Minimum value that the smallest cutpoint may take.
    """

    def __init__(self, net: nn.Module, margin: float = 0.0, min_val: float = -1.0e6) -> None:
        super().__init__()
        self.net = net
        self.margin = margin
        self.min_val = min_val

    def clip(self, module: Module) -> None:
        # NOTE: Only works for LogisticCumulativeLink right now
        # We assume the cutpoints parameters are called `cutpoints`.
        if isinstance(module, LogisticCumulativeLink):
            cutpoints = module.cutpoints.data
            for i in range(cutpoints.shape[0] - 1):
                cutpoints[i].clamp_(self.min_val,
                                    cutpoints[i + 1] - self.margin)

    def on_batch_end(self, state: RunnerState):
        self.net.apply(self.clip)


class NegativeMiningCallback(Callback):

    def __init__(
            self,
            input_key: str = "targets",
            output_key: str = "logits",
            from_regression=False,
            ignore_index=None
    ):
        """
        :param input_key: input key to use for precision calculation;
            specifies our `y_true`.
        :param output_key: output key to use for precision calculation;
            specifies our `y_pred`.
        """
        self.output_key = output_key
        self.input_key = input_key
        self.from_regression = from_regression
        self.image_ids = []
        self.y_preds = []
        self.y_preds_raw = []
        self.y_trues = []
        self.ignore_index = ignore_index

    def on_loader_start(self, state: RunnerState):
        self.image_ids = []
        self.y_preds_raw = []
        self.y_preds = []
        self.y_trues = []

    def on_loader_end(self, state: RunnerState):
        df = pd.DataFrame.from_dict({
            'image_id': self.image_ids,
            'y_true': self.y_trues,
            'y_pred': self.y_preds,
            'y_pred_raw': self.y_preds_raw
        })

        fname = os.path.join(state.logdir, 'negatives', state.loader_name, f'epoch_{state.epoch}.csv')
        os.makedirs(os.path.dirname(fname), exist_ok=True)
        df.to_csv(fname, index=None)

    def on_batch_end(self, state: RunnerState):
        y_true = state.input[self.input_key].detach()
        y_pred_raw = state.output[self.output_key].detach()

        if self.from_regression:
            y_pred = regression_to_class(y_pred_raw)
        else:
            y_pred = torch.argmax(y_pred_raw, dim=1)

        y_pred_raw = to_numpy(y_pred_raw)
        y_pred = to_numpy(y_pred).astype(int)
        y_true = to_numpy(y_true).astype(int)
        image_ids = np.array(state.input['image_id'])

        if self.ignore_index is not None:
            mask = y_true != self.ignore_index
            y_pred_raw = y_pred_raw[mask]
            y_pred = y_pred[mask]
            y_true = y_true[mask]
            image_ids = image_ids[mask]
            if len(y_true) == 0:
                return

        negatives = y_true != y_pred

        self.image_ids.extend(image_ids[negatives])
        self.y_preds_raw.extend(y_pred_raw[negatives])
        self.y_preds.extend(y_pred[negatives])
        self.y_trues.extend(y_true[negatives])


class LinearWeightDecayCallback(Callback):
    """
    Linearly increase weight decay factor after each epoch by @step
    """

    def __init__(self, step=5e-4):
        self.step = step
        self.wd_state = None

    def on_stage_start(self, state: RunnerState):
        self.wd_state = [pg["weight_decay"] for pg in state.optimizer.param_groups]

    def on_epoch_start(self, state: RunnerState):
        for i, pg in enumerate(state.optimizer.param_groups):
            pg["weight_decay"] += self.step

            state.metrics.epoch_values['train'][f'optimizer/{i}/weight_decay'] = pg["lr"]
            state.metrics.epoch_values['train'][f'optimizer/{i}/weight_decay'] = pg["weight_decay"]

    def on_stage_end(self, state: RunnerState):
        for pg, wd in zip(state.optimizer.param_groups, self.wd_state):
            pg["weight_decay"] = wd


class CustomOptimizerCallback(OptimizerCallback):
    """
        Optimizer callback, abstraction over optimizer step.
        Commented hacky weight decay
        """

    def __init__(self, grad_clip_params: Dict = None, accumulation_steps: int = 1, optimizer_key: str = None,
                 loss_key: str = None, prefix: str = None):
        """
        @TODO: docs
        """

        super().__init__(grad_clip_params, accumulation_steps, optimizer_key, loss_key, prefix)

    @staticmethod
    def grad_step(*, optimizer, optimizer_wd=0, grad_clip_fn=None):
        for group in optimizer.param_groups:
            # if optimizer_wd > 0:
            #     for param in group["params"]:
            #         param.data = param.data.add(
            #             -optimizer_wd * group["lr"], param.data
            #         )
            if grad_clip_fn is not None:
                grad_clip_fn(group["params"])
        optimizer.step()

    def on_stage_start(self, state: RunnerState):
        optimizer = state.get_key(
            key="optimizer", inner_key=self.optimizer_key
        )
        assert optimizer is not None
        lr = optimizer.defaults["lr"]
        momentum = get_optimizer_momentum(optimizer)
        state.set_key(lr, "lr", inner_key=self.optimizer_key)
        state.set_key(momentum, "momentum", inner_key=self.optimizer_key)

    def on_epoch_start(self, state):
        # optimizer = state.get_key(
        #     key="optimizer", inner_key=self.optimizer_key
        # )
        # self._optimizer_wd = optimizer.param_groups[0].get("weight_decay", 0.0)
        # optimizer.param_groups[0]["weight_decay"] = 0.0
        pass

    def on_batch_start(self, state):
        state.loss = None

    def on_batch_end(self, state):
        loss = state.get_key(key="loss", inner_key=self.loss_key)
        if isinstance(loss, dict):
            loss = list(loss.values())
        if isinstance(loss, list):
            loss = torch.mean(torch.stack(loss))

        if self.prefix is not None:
            state.metrics.add_batch_value(metrics_dict={
                self.prefix: loss.item(),
            })

        if not state.need_backward:
            return

        self._accumulation_counter += 1
        model = state.model
        optimizer = state.get_key(
            key="optimizer", inner_key=self.optimizer_key
        )

        # This is very hacky check whether we have AMP optimizer and this may
        # change in future.
        # But alternative solution is to have AmpOptimizerCallback.
        # or expose another c'tor argument.
        if hasattr(optimizer, "_amp_stash"):
            from apex import amp
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        if (self._accumulation_counter + 1) % self.accumulation_steps == 0:
            self.grad_step(
                optimizer=optimizer,
                optimizer_wd=self._optimizer_wd,
                grad_clip_fn=self.grad_clip_fn
            )
            model.zero_grad()
            self._accumulation_counter = 0

    def on_epoch_end(self, state):
        # optimizer = state.get_key(
        #     key="optimizer", inner_key=self.optimizer_key
        # )
        # optimizer.param_groups[0]["weight_decay"] = self._optimizer_wd
        pass
