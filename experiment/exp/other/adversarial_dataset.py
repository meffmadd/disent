#  ~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~
#  MIT License
#
#  Copyright (c) 2021 Nathan Juraj Michlo
#
#  Permission is hereby granted, free of charge, to any person obtaining a copy
#  of this software and associated documentation files (the "Software"), to deal
#  in the Software without restriction, including without limitation the rights
#  to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
#  copies of the Software, and to permit persons to whom the Software is
#  furnished to do so, subject to the following conditions:
#
#  The above copyright notice and this permission notice shall be included in
#  all copies or substantial portions of the Software.
#
#  THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
#  IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
#  FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
#  AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
#  LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
#  OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
#  SOFTWARE.
#  ~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~=~
import numpy as np
import torch_optimizer
import torchsort
from matplotlib import pyplot as plt
from torch.optim.lr_scheduler import ReduceLROnPlateau
from torch.utils.data import DataLoader
from tqdm import tqdm

from disent.data.groundtruth import Cars3dData
from disent.data.groundtruth import Shapes3dData
from disent.data.groundtruth import XYSquaresData
import torch
import torch.nn.functional as F

from disent.dataset.groundtruth import GroundTruthDataset
from disent.transform import ToStandardisedTensor
from disent.transform.functional import conv2d_channel_wise_fft
from disent.util import seed
from disent.util.math import get_kernel_size
from disent.util.math import torch_box_kernel_2d
from disent.util.math import torch_gaussian_kernel_2d
from disent.util.math_loss import multi_spearman_rank_loss
from disent.util.math_loss import spearman_rank_loss


# ========================================================================= #
# helper                                                                    #
# ========================================================================= #
from disent.util.math_loss import torch_mse_rank_loss
from disent.util.math_loss import torch_soft_rank
from disent.util.math_loss import torch_soft_sort


def make_optimizer(model: torch.nn.Module, name: str = 'sgd', lr=1e-3):
    if isinstance(model, torch.nn.Module):
        params = model.parameters()
    elif isinstance(model, torch.Tensor):
        assert model.requires_grad
        params = [model]
    else:
        raise TypeError(f'cannot optimize type: {type(model)}')
    # make optimizer
    if name == 'sgd': return torch.optim.SGD(params, lr=lr)
    elif name == 'sgd_m': return torch.optim.SGD(params, lr=lr, momentum=0.1)
    elif name == 'adam': return torch.optim.Adam(params, lr=lr)
    elif name == 'radam': return torch_optimizer.RAdam(params, lr=lr)
    else: raise KeyError(f'invalid optimizer name: {repr(name)}')


def make_dataset(name: str = 'xysquares', dataloader=False):
    if name == 'xysquares':  dataset = GroundTruthDataset(XYSquaresData(), transform=ToStandardisedTensor())
    elif name == 'xysquares_1x1':  dataset = GroundTruthDataset(XYSquaresData(square_size=1), transform=ToStandardisedTensor())
    elif name == 'xysquares_2x2':  dataset = GroundTruthDataset(XYSquaresData(square_size=2), transform=ToStandardisedTensor())
    elif name == 'xysquares_4x4':  dataset = GroundTruthDataset(XYSquaresData(square_size=4), transform=ToStandardisedTensor())
    elif name == 'cars3d':   dataset = GroundTruthDataset(Cars3dData(),    transform=ToStandardisedTensor(size=64))
    elif name == 'shapes3d': dataset = GroundTruthDataset(Shapes3dData(),  transform=ToStandardisedTensor())
    else: raise KeyError(f'invalid data name: {repr(name)}')
    return dataset


def step_optimizer(optimizer, loss):
    optimizer.zero_grad()
    loss.backward()
    optimizer.step()


def get_single_batch(dataloader, cuda=True):
    for batch in dataloader:
        (x_targ,) = batch['x_targ']
        break
    if cuda:
        x_targ = x_targ.cuda()
    return x_targ


def to_img(x, scale=False):
    assert x.dtype in {torch.float16, torch.float32, torch.float64, torch.complex32, torch.complex64}, f'unsupported dtype: {x.dtype}'
    x = x.detach().cpu()
    x = torch.abs(x)
    if scale:
        m, M = torch.min(x), torch.max(x)
        x = (x - m) / (M - m)
    x = torch.moveaxis(x, 0, -1)
    x = torch.clamp(x, 0, 1)
    x = (x * 255).to(torch.uint8)
    return x


def show_img(x, scale=False, i=None, step=None):
    if (i is None) or (step is None) or (i % step == 0):
        plt.imshow(to_img(x, scale=scale))
        plt.axis('off')
        plt.tight_layout()
        plt.show()


def show_imgs(xs, scale=False, i=None, step=None):
    if (i is None) or (step is None) or (i % step == 0):
        n = int(np.ceil(np.sqrt(len(xs))))
        fig, axs = plt.subplots(n, n)
        for ax, im in zip(np.array(axs).flatten(), xs):
            ax.imshow(to_img(im, scale=scale))
            ax.set_axis_off()
        plt.tight_layout()
        plt.show()


# ========================================================================= #
# tests                                                                     #
# ========================================================================= #


def run_differentiable_sorting_loss(dataset='xysquares', loss_mode='spearman', optimizer='adam', lr=1e-2):
    """
    test that the differentiable sorting works over a batch of images.
    """

    dataset = make_dataset(dataset)
    dataloader = DataLoader(dataset=dataset, batch_size=256, pin_memory=True, shuffle=True)

    y = get_single_batch(dataloader)
    # y += torch.randn_like(y) * 0.001  # prevent nan errors
    x = torch.randn_like(y, requires_grad=True)

    optimizer = make_optimizer(x, name=optimizer, lr=lr)

    for i in range(1001):
        if loss_mode == 'spearman':
            loss = multi_spearman_rank_loss(x, y, dims=(2, 3), nan_to_num=True)
        elif loss_mode == 'mse_rank':
            loss = 0.
            loss += F.mse_loss(torch_soft_rank(x, dims=(-3, -1)), torch_soft_rank(y, dims=(-3, -1)), reduction='mean')
            loss += F.mse_loss(torch_soft_rank(x, dims=(-3, -2)), torch_soft_rank(y, dims=(-3, -2)), reduction='mean')
        elif loss_mode == 'mse':
            loss += F.mse_loss(x, y, reduction='mean')
        else:
            raise KeyError(f'invalid loss mode: {repr(loss_mode)}')

        # update variables
        step_optimizer(optimizer, loss)
        show_img(x[0], i=i, step=250)

        # compute loss
        print(i, float(loss))


def unreduced_mse_loss(pred, targ) -> torch.Tensor:
    return F.mse_loss(pred, targ, reduction='none')

def unreduced_mae_loss(pred, targ) -> torch.Tensor:
    return torch.abs(pred - targ)

def unreduced_msae_loss(pred, targ) -> torch.Tensor:
    return torch.abs(pred - targ) + F.mse_loss(pred, targ, reduction='none')

def unreduced_loss(pred, targ, mode='mse') -> torch.Tensor:
    return _LOSS_FN[mode](pred, targ)

_LOSS_FN = {
    'mse': unreduced_mse_loss,
    'mae': unreduced_mae_loss,
    'msae': unreduced_msae_loss,
}


def stochastic_const_loss(pred: torch.Tensor, targ: torch.Tensor, mask: torch.Tensor, num_obs, num_samples, loss='mse', reg_out_of_bounds=True, top_k: int = None) -> torch.Tensor:
    ia, ib = torch.randint(0, num_obs, size=(2, num_samples), device=pred.device)
    iA, iB = torch.randint(0, num_samples, size=(2, num_samples), device=pred.device)
    # constant dist loss
    x_ds = (unreduced_loss(pred[ia], pred[ib], mode=loss) * mask[None, ...]).mean(dim=(-3, -2, -1))
    if top_k is None:
        lcst = unreduced_loss(x_ds[iA], x_ds[iB], mode=loss).mean()
    else:
        lcst = unreduced_loss(x_ds[iA], x_ds[iB], mode=loss)
        lcst = torch.topk(lcst, k=top_k, largest=True).values.mean()
    # values over the required range
    if reg_out_of_bounds:
        m = torch.nan_to_num((0 - pred[pred < 0]) ** 2, nan=0).mean()
        M = torch.nan_to_num((pred[pred > 1] - 1) ** 2, nan=0).mean()
        mM = m + M
    else:
        mM = 0.
    # done!
    return mM + lcst


def get_factor_idx(dataset, factor) -> int:
    if isinstance(factor, str):
        try:
            f_idx = dataset.factor_names.index(factor)
        except:
            raise KeyError(f'{repr(factor)} is not one of: {dataset.factor_names}')
    else:
        assert isinstance(factor, int)
        f_idx = factor
    return f_idx


def sample_factors(dataset, num_obs=1024, factor_mode='sample_random', factor: str = None):
    # sample multiple random factor traversals
    if factor_mode == 'sample_traversals':
        assert factor is not None, f'factor cannot be None when factor_mode=={repr(factor_mode)}'
        # get traversal
        f_idx = get_factor_idx(dataset, factor)
        # generate traversals
        factors = []
        for i in range((num_obs + dataset.factor_sizes[f_idx] - 1) // dataset.factor_sizes[f_idx]):
            factors.append(dataset.sample_random_traversal_factors(f_idx=f_idx))
        factors = np.concatenate(factors, axis=0)
    elif factor_mode == 'sample_random':
        factors = dataset.sample_factors(num_obs)
    else:
        raise KeyError
    return factors


def make_changed_mask(batch, masked=True):
    if masked:
        mask = torch.zeros_like(batch[0], dtype=torch.bool)
        for i in range(len(batch)):
            mask |= (batch[0] != batch[i])
    else:
        mask = torch.ones_like(batch[0], dtype=torch.bool)
    return mask


def run_generate_adversarial_data(
    dataset='shapes3d',
    factor='wall_hue',
    factor_mode='sample_random',
    optimizer='adam',
    lr=1e-2,
    num_obs=2048,
    num_samples=1024,
    noise_weight=0.01,
    masked=True,
    loss_fn='mse',
    loss_top_k=128,
    reg_out_of_bounds=False,
    steps=2000,
    display_period=500,
):
    seed(777)
    # make dataset
    dataset = make_dataset(dataset)
    # make batches
    factors = sample_factors(dataset, num_obs=num_obs, factor_mode=factor_mode, factor=factor)
    batch = dataset.dataset_batch_from_factors(factors, 'target')
    if torch.cuda.is_available():
        batch = batch.cuda()
    x = torch.tensor(batch + torch.randn_like(batch) * noise_weight, requires_grad=True)
    # generate mask
    mask = make_changed_mask(batch, masked=masked)
    show_img(mask.to(torch.float32))
    # make optimizer
    optimizer = make_optimizer(x, name=optimizer, lr=lr)

    # optimize differences according to loss
    prog = tqdm(range(steps+1), postfix={'loss': 0.0})
    for i in prog:
        # final loss
        loss = stochastic_const_loss(x, batch, mask, num_obs=num_obs, num_samples=num_samples, loss=loss_fn, reg_out_of_bounds=reg_out_of_bounds, top_k=loss_top_k)
        # update variables
        step_optimizer(optimizer, loss)
        show_imgs(x[:9], i=i, step=display_period)
        prog.set_postfix({'loss': float(loss)})


def spearman_rank_dist(
    pred: torch.Tensor,
    targ: torch.Tensor,
    reduction='mean',
    nan_to_num=False,
):
    # add missing dim
    if pred.ndim == 1:
        pred, targ = pred.reshape(1, -1), targ.reshape(1, -1)
    assert pred.shape == targ.shape
    assert pred.ndim == 2
    # sort the last dimension of the 2D tensors
    pred = torch.argsort(pred).to(torch.float32)
    targ = torch.argsort(targ).to(torch.float32)
    # compute individual losses
    # TODO: this can result in nan values, what to do then?
    pred = pred - pred.mean(dim=-1, keepdim=True)
    pred = pred / pred.norm(dim=-1, keepdim=True)
    targ = targ - targ.mean(dim=-1, keepdim=True)
    targ = targ / targ.norm(dim=-1, keepdim=True)
    # replace nan values
    if nan_to_num:
        pred = torch.nan_to_num(pred, nan=0.0)
        targ = torch.nan_to_num(targ, nan=0.0)
    # compute the final loss
    loss = (pred * targ).sum(dim=-1)
    # reduce the loss
    if reduction == 'mean':
        return loss.mean()
    elif reduction == 'none':
        return loss
    else:
        raise KeyError(f'Invalid reduction mode: {repr(reduction)}')


def check_xy_squares_dists(kernel='box', repeats=100, samples=256, pairwise_samples=256, kernel_radius=32, show_prog=True):
    if kernel == 'box':
        kernel = torch_box_kernel_2d(radius=kernel_radius)[None, ...]
    elif kernel == 'max_box':
        crange = torch.abs(torch.arange(kernel_radius * 2 + 1) - kernel_radius)
        y, x = torch.meshgrid(crange, crange)
        d = torch.maximum(x, y) + 1
        d = d.max() - d
        kernel = (d.to(torch.float32) / d.sum())[None, None, ...]
    elif kernel == 'min_box':
        crange = torch.abs(torch.arange(kernel_radius * 2 + 1) - kernel_radius)
        y, x = torch.meshgrid(crange, crange)
        d = torch.minimum(x, y) + 1
        d = d.max() - d
        kernel = (d.to(torch.float32) / d.sum())[None, None, ...]
    elif kernel == 'manhat_box':
        crange = torch.abs(torch.arange(kernel_radius * 2 + 1) - kernel_radius)
        y, x = torch.meshgrid(crange, crange)
        d = (y + x) + 1
        d = d.max() - d
        kernel = (d.to(torch.float32) / d.sum())[None, None, ...]
    elif kernel == 'gaussian':
        kernel = torch_gaussian_kernel_2d(sigma=kernel_radius / 4.0, truncate=4.0)[None, None, ...]
    else:
        raise KeyError(f'invalid kernel mode: {repr(kernel)}')

    # make dataset
    dataset = make_dataset('xysquares')

    losses = []
    prog = tqdm(range(repeats), postfix={'loss': 0.0}) if show_prog else range(repeats)

    for i in prog:
        # get random samples
        factors = dataset.sample_factors(samples)
        batch = dataset.dataset_batch_from_factors(factors, mode='target')
        if torch.cuda.is_available():
            batch = batch.cuda()
            kernel = kernel.cuda()
        factors = torch.from_numpy(factors).to(dtype=torch.float32, device=batch.device)

        # random pairs
        ia, ib = torch.randint(0, len(batch), size=(2, pairwise_samples), device=batch.device)

        # compute factor distances
        f_dists = torch.abs(factors[ia] - factors[ib]).sum(dim=-1)

        # compute loss distances
        aug_batch = conv2d_channel_wise_fft(batch, kernel)
        # TODO: aug - batch or aug - aug
        # b_dists = torch.abs(aug_batch[ia] - aug_batch[ib]).sum(dim=(-3, -2, -1))
        b_dists = F.mse_loss(aug_batch[ia], aug_batch[ib], reduction='none').sum(dim=(-3, -2, -1))

        # compute ranks
        # losses.append(float(torch.clamp(torch_mse_rank_loss(b_dists, f_dists), 0, 100)))
        # losses.append(float(torch.abs(torch.argsort(f_dists, descending=True) - torch.argsort(b_dists, descending=False)).to(torch.float32).mean()))
        losses.append(float(spearman_rank_dist(b_dists, f_dists)))

        if show_prog:
            prog.set_postfix({'loss': np.mean(losses)})

    return np.mean(losses), aug_batch[0]


def run_check_all_xy_squares_dists(show=False):
    for kernel in [
        'box',
        'max_box',
        'min_box',
        'manhat_box',
        'gaussian',
    ]:
        rs = list(range(1, 33, 4))
        ys = []
        for r in rs:
            ave_spearman, last_img = check_xy_squares_dists(kernel=kernel, repeats=32, samples=128, pairwise_samples=1024, kernel_radius=r, show_prog=False)
            if show:
                show_img(last_img, scale=True)
            ys.append(abs(ave_spearman))
            print(kernel, r, ':', r*2+1, abs(ave_spearman))
        plt.plot(rs, ys, label=kernel)
    plt.legend()
    plt.show()


def sample_batch_and_factors(dataset, num_samples, factor_mode='sample_random', factor=None, device=None):
    factors = sample_factors(dataset, num_obs=num_samples, factor_mode=factor_mode, factor=factor)
    batch = dataset.dataset_batch_from_factors(factors, mode='target').to(device=device)
    factors = torch.from_numpy(factors).to(dtype=torch.float32, device=device)
    return batch, factors


def train_kernel_to_disentangle_xy(
    dataset='xysquares_1x1',
    kernel_radius=33,
    kernel_channels=False,
    batch_size=128,
    batch_samples_ratio=4.0,
    batch_factor_mode='sample_random',  # sample_random, sample_traversals
    batch_factor=None,
    batch_aug_both=True,
    train_steps=10000,
    train_optimizer='radam',
    train_lr=1e-3,
    loss_dist_mse=True,
    progress=True,
):
    device = 'cuda' if torch.cuda.is_available() else 'cpu'
    # make dataset
    dataset = make_dataset(dataset)
    # make trainable kernel
    kernel = torch.abs(torch.randn(1, 3 if kernel_channels else 1, 2*kernel_radius+1, 2*kernel_radius+1, dtype=torch.float32, device=device))
    kernel = kernel / kernel.sum(dim=(0, 2, 3), keepdim=True)
    kernel = torch.tensor(kernel, device=device, requires_grad=True)
    # make optimizer
    optimizer = make_optimizer(kernel, name=train_optimizer, lr=train_lr)
    schedule = ReduceLROnPlateau(optimizer)

    # factor to optimise
    f_idx = get_factor_idx(dataset, batch_factor) if (batch_factor is not None) else None

    # train
    pbar = tqdm(range(train_steps+1), postfix={'loss': 0.0}, disable=not progress)
    for i in pbar:
        batch, factors = sample_batch_and_factors(dataset, num_samples=batch_size, factor_mode=batch_factor_mode, factor=batch_factor, device=device)
        # random pairs
        ia, ib = torch.randint(0, len(batch), size=(2, int(batch_size * batch_samples_ratio)), device=batch.device)
        # compute loss distances
        aug_batch = conv2d_channel_wise_fft(batch, kernel)
        (targ_a, targ_b) = (aug_batch[ia], aug_batch[ib]) if batch_aug_both else (aug_batch[ia], batch[ib])
        if loss_dist_mse:
            b_dists = F.mse_loss(targ_a, targ_b, reduction='none').sum(dim=(-3, -2, -1))
        else:
            b_dists = torch.abs(targ_a - targ_b).sum(dim=(-3, -2, -1))
        # compute factor distances
        if f_idx:
            f_dists = torch.abs(factors[ia, f_idx] - factors[ib, f_idx])
        else:
            f_dists = torch.abs(factors[ia] - factors[ib]).sum(dim=-1)
        # optimise metric
        loss = spearman_rank_loss(b_dists, -f_dists)  # decreasing overlap should mean increasing factor dist
        # ~=~=~=~=~=~=~=~=~=~=~=~=~=~=~ #
        # update variables
        step_optimizer(optimizer, loss)
        show_img(kernel[0], i=i, step=100, scale=True)
        pbar.set_postfix({'loss': float(loss)})



# ========================================================================= #
# entrypoint                                                                #
# ========================================================================= #


if __name__ == '__main__':
    # run_generate_adversarial_data()
    # run_differentiable_sorting_loss()
    # run_check_all_xy_squares_dists()
    train_kernel_to_disentangle_xy()
