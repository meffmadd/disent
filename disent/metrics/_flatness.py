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

"""
Flatness Metric
- Nathan Michlo et. al
"""

import logging
from typing import Iterable
from typing import Union

import numpy as np
import torch
from torch.utils.data.dataloader import default_collate

from disent.dataset.groundtruth import GroundTruthDataset
from disent.util import chunked


log = logging.getLogger(__name__)


# ========================================================================= #
# flatness                                                                  #
# ========================================================================= #


def metric_flatness(
        ground_truth_dataset: GroundTruthDataset,
        representation_function: callable,
        factor_repeats: int = 1024,
        batch_size: int = 64,
):
    """
    Computes the flatness metric:
        approximately equal to: total_dim_width / (ave_point_dist_along_dim * num_points_along_dim)

    Complexity of this metric is:
        O(num_factors * ave_factor_size * repeats)
        eg. 9 factors * 64 indices on ave * 128 repeats = 73728 observations loaded from the dataset

    factor_repeats:
      - can go all the way down to about 64 and still get decent results.
      - 64 is accurate to about +- 0.01
      - 128 is accurate to about +- 0.003
      - 1024 is accurate to about +- 0.001

    Args:
      ground_truth_dataset: GroundTruthData to be sampled from.
      representation_function: Function that takes observations as input and outputs a dim_representation sized representation for each observation.
      factor_repeats: how many times to repeat a traversal along each factors, these are then averaged together.
      batch_size: Batch size to process at any time while generating representations, should not effect metric results.
      p: how to calculate distances in the latent space, see torch.norm
    Returns:
      Dictionary with average disentanglement score, completeness and
        informativeness (train and test).
    """
    p_fs_measures = aggregate_measure_distances_along_all_factors(ground_truth_dataset, representation_function, repeats=factor_repeats, batch_size=batch_size, ps=(1, 2))
    # aggregate data
    results = {
        'flatness.ave_flatness':    compute_flatness(widths=p_fs_measures[2]['fs_ave_widths'], lengths=p_fs_measures[1]['fs_ave_lengths'], factor_sizes=ground_truth_dataset.factor_sizes),
        'flatness.ave_flatness_l1': compute_flatness(widths=p_fs_measures[1]['fs_ave_widths'], lengths=p_fs_measures[1]['fs_ave_lengths'], factor_sizes=ground_truth_dataset.factor_sizes),
        'flatness.ave_flatness_l2': compute_flatness(widths=p_fs_measures[2]['fs_ave_widths'], lengths=p_fs_measures[2]['fs_ave_lengths'], factor_sizes=ground_truth_dataset.factor_sizes),
        # distances
        'flatness.ave_width_l1':    torch.mean(filter_inactive_factors(p_fs_measures[1]['fs_ave_widths'], factor_sizes=ground_truth_dataset.factor_sizes)),
        'flatness.ave_width_l2':    torch.mean(filter_inactive_factors(p_fs_measures[2]['fs_ave_widths'], factor_sizes=ground_truth_dataset.factor_sizes)),
        'flatness.ave_length_l1':   torch.mean(filter_inactive_factors(p_fs_measures[1]['fs_ave_lengths'], factor_sizes=ground_truth_dataset.factor_sizes)),
        'flatness.ave_length_l2':   torch.mean(filter_inactive_factors(p_fs_measures[2]['fs_ave_lengths'], factor_sizes=ground_truth_dataset.factor_sizes)),
    }
    # convert values from torch
    return {k: float(v) for k, v in results.items()}


def compute_flatness(widths, lengths, factor_sizes):
    widths = filter_inactive_factors(widths, factor_sizes)
    lengths = filter_inactive_factors(lengths, factor_sizes)
    # checks
    assert torch.all(widths >= 0)
    assert torch.all(lengths >= 0)
    assert torch.all(torch.eq(widths == 0, lengths == 0))
    # update scores
    widths[lengths == 0] = 0
    lengths[lengths == 0] = 1
    # compute flatness
    return (widths / lengths).mean()


def filter_inactive_factors(tensor, factor_sizes):
    factor_sizes = torch.tensor(factor_sizes, device=tensor.device)
    assert torch.all(factor_sizes >= 1)
    # remove
    active_factors = torch.nonzero(factor_sizes-1, as_tuple=True)
    return tensor[active_factors]


def aggregate_measure_distances_along_all_factors(
        ground_truth_dataset,
        representation_function,
        repeats: int,
        batch_size: int,
        ps: Iterable[Union[str, int]] = (1, 2),
) -> dict:
    # COMPUTE AGGREGATES FOR EACH FACTOR
    # -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~- #
    fs_p_measures = [
        aggregate_measure_distances_along_factor(ground_truth_dataset, representation_function, f_idx=f_idx, repeats=repeats, batch_size=batch_size, ps=ps)
        for f_idx in range(ground_truth_dataset.num_factors)
    ]

    # FINALIZE FOR EACH FACTOR
    # -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~- #
    p_fs_measures = {}
    for p, fs_measures in default_collate(fs_p_measures).items():
        fs_ave_widths = fs_measures['ave_width']
        # get number of spaces deltas (number of points minus 1)
        # compute length: estimated version of factors_ave_width = factors_num_deltas * factors_ave_delta
        fs_num_deltas = torch.as_tensor(ground_truth_dataset.factor_sizes, device=fs_ave_widths.device) - 1
        fs_ave_deltas = fs_measures['ave_delta']
        fs_ave_lengths = fs_num_deltas * fs_ave_deltas
        # update
        p_fs_measures[p] = {'fs_ave_widths': fs_ave_widths, 'fs_ave_lengths': fs_ave_lengths}
    return p_fs_measures


def aggregate_measure_distances_along_factor(
        ground_truth_dataset,
        representation_function,
        f_idx: int,
        repeats: int,
        batch_size: int,
        ps: Iterable[Union[str, int]] = (1, 2),
        cycle_fail: bool = False,
) -> dict:
    f_size = ground_truth_dataset.factor_sizes[f_idx]

    if f_size == 1:
        if cycle_fail:
            raise ValueError(f'dataset factor size is too small for flatness metric with cycle_normalize enabled! size={f_size} < 2')
        device = get_device(ground_truth_dataset, representation_function)
        return {p: {'ave_width': torch.as_tensor(0., device=device), 'ave_delta': torch.as_tensor(0., device=device)} for p in ps}

    # FEED FORWARD, COMPUTE ALL DELTAS & WIDTHS - For each distance measure
    # -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~- #
    p_measures: list = [{} for _ in range(repeats)]
    for measures in p_measures:
        # generate repeated factors, varying one factor over the entire range
        zs_traversal = encode_all_along_factor(ground_truth_dataset, representation_function, f_idx=f_idx, batch_size=batch_size)
        # for each distance measure compute everything
        # - width: calculate the distance between the furthest two points
        # - deltas: calculating the distances of their representations to the next values.
        # - cycle_normalize: we cant get the ave next dist directly because of cycles, so we remove the largest dist
        for p in ps:
            width      = knn(x=zs_traversal, y=zs_traversal, k=1, largest=True, p=p).values.max()      # shape: (,)
            deltas     = torch.norm(zs_traversal - torch.roll(zs_traversal, -1, dims=0), dim=-1, p=p)  # shape: (factor_size,)
            deltas = torch.topk(deltas, k=f_size-1, dim=-1, largest=False, sorted=False).values        # shape: (factor_size-1,)
            measures[p] = {'width': width, 'deltas': deltas}

    # AGGREGATE DATA - For each distance measure
    # -~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~-~- #
    return {
        p: {
            'ave_width': measures['width'].mean(dim=0),        # shape: (repeats,) -> ()
            'ave_delta': measures['deltas'].mean(dim=[0, 1]),  # shape: (repeats, factor_size - 1) -> ()
        } for p, measures in default_collate(p_measures).items()
    }


# ========================================================================= #
# ENCODE                                                                    #
# ========================================================================= #


def encode_all_along_factor(ground_truth_dataset, representation_function, f_idx: int, batch_size: int):
    f_size = ground_truth_dataset.factor_sizes[f_idx]
    # generate repeated factors, varying one factor over a range (f_size, f_dims)
    factors = range_along_repeated_factors(ground_truth_dataset, idx=f_idx, num=f_size)
    # get the representations of all the factors (f_size, z_size)
    sequential_zs = encode_all_factors(ground_truth_dataset, representation_function, factors=factors, batch_size=batch_size)
    return sequential_zs


def range_along_repeated_factors(ground_truth_dataset, idx: int, num: int) -> np.ndarray:
    # Aka. a traversal along a single factor
    # make sequential factors, one randomly sampled list of
    # factors, then repeated, with one index mutated as if set by range()
    factors = ground_truth_dataset.sample_factors(size=1)
    factors = factors.repeat(num, axis=0)
    factors[:, idx] = np.arange(num)
    return factors


def encode_all_factors(ground_truth_dataset, representation_function, factors, batch_size: int) -> torch.Tensor:
    zs = []
    with torch.no_grad():
        for batch_factors in chunked(factors, chunk_size=batch_size):
            batch = ground_truth_dataset.dataset_batch_from_factors(batch_factors, mode='input')
            z = representation_function(batch)
            zs.append(z)
    return torch.cat(zs, dim=0)


def get_device(ground_truth_dataset, representation_function):
    # this is a hack...
    return representation_function(ground_truth_dataset.dataset_sample_batch(1, mode='input')).device


# ========================================================================= #
# DISTANCES                                                                 #
# ========================================================================= #


def knn(x, y, k: int = None, largest=False, p='fro'):
    assert 0 < k <= y.shape[0]
    # check input vectors, must be array of vectors
    assert 2 == x.ndim == y.ndim
    assert x.shape[1:] == y.shape[1:]
    # compute distances between each and every pair
    dist_mat = x[:, None, ...] - y[None, :, ...]
    dist_mat = torch.norm(dist_mat, dim=-1, p=p)
    # return closest distances
    return torch.topk(dist_mat, k=k, dim=-1, largest=largest, sorted=True)


# ========================================================================= #
# END                                                                       #
# ========================================================================= #


# if __name__ == '__main__':
#     import pytorch_lightning as pl
#     from torch.optim import Adam
#     from torch.utils.data import DataLoader
#     from disent.data.groundtruth import XYObjectData, XYSquaresData
#     from disent.dataset.groundtruth import GroundTruthDataset
#     from disent.frameworks.vae.unsupervised import BetaVae
#     from disent.model.ae import EncoderConv64, DecoderConv64, AutoEncoder
#     from disent.transform import ToStandardisedTensor
#
#     def get_str(r):
#         return ', '.join(f'{k}={v:6.4f}' for k, v in r.items())
#
#     def print_r(name, steps, result, clr=colors.lYLW, t: Timer = None):
#         print(f'{clr}{name:<13} ({steps:>04}){f" {colors.GRY}[{t.pretty}]{clr}" if t else ""}: {get_str(result)}{colors.RST}')
#
#     def calculate(name, steps, dataset, get_repr):
#         global aggregate_measure_distances_along_factor
#         with Timer() as t_A: r_A = metric_flatness(dataset, get_repr, factor_repeats=64, batch_size=64)
#         with Timer() as t_B: r_B = metric_flatness(dataset, get_repr, factor_repeats=64, batch_size=64)
#         results.append((name, steps, r_A, r_B))
#         print_r(name + '_A', steps, r_A, colors.lRED, t=t_A)
#         print_r(name + '_B', steps, r_B, colors.lRED, t=t_B)
#         print(colors.GRY, '='*100, colors.RST, sep='')
#         return r_A, r_B
#
#     class XYOverlapData(XYSquaresData):
#         def __init__(self, square_size=8, grid_size=64, grid_spacing=None, num_squares=3, rgb=True):
#             if grid_spacing is None:
#                 grid_spacing = (square_size+1) // 2
#             super().__init__(square_size=square_size, grid_size=grid_size, grid_spacing=grid_spacing, num_squares=num_squares, rgb=rgb)
#
#     results = []
#     for data in [XYObjectData(rgb=False, palette='white'), XYSquaresData(), XYOverlapData(), XYObjectData()]:
#         dataset = GroundTruthDataset(data, transform=ToStandardisedTensor())
#         dataloader = DataLoader(dataset=dataset, batch_size=32, shuffle=True, pin_memory=True)
#         module = BetaVae(
#             make_optimizer_fn=lambda params: Adam(params, lr=5e-4),
#             make_model_fn=lambda: AutoEncoder(
#                 encoder=EncoderConv64(x_shape=data.x_shape, z_size=6, z_multiplier=2),
#                 decoder=DecoderConv64(x_shape=data.x_shape, z_size=6),
#             ),
#             cfg=BetaVae.cfg(beta=1)
#         )
#         # we cannot guarantee which device the representation is on
#         get_repr = lambda x: module.encode(x.to(module.device))
#         # PHASE 1, UNTRAINED
#         pl.Trainer(logger=False, checkpoint_callback=False, fast_dev_run=True, gpus=1, weights_summary=None).fit(module, dataloader)
#         module = module.to('cuda')
#         calculate(data.__class__.__name__, 0, dataset, get_repr)
#         # PHASE 2, LITTLE TRAINING
#         pl.Trainer(logger=False, checkpoint_callback=False, max_steps=256, gpus=1, weights_summary=None).fit(module, dataloader)
#         calculate(data.__class__.__name__, 256, dataset, get_repr)
#         # PHASE 3, MORE TRAINING
#         pl.Trainer(logger=False, checkpoint_callback=False, max_steps=2048, gpus=1, weights_summary=None).fit(module, dataloader)
#         calculate(data.__class__.__name__, 256+2048, dataset, get_repr)
#         results.append(None)
#
#     for result in results:
#         if result is None:
#             print()
#             continue
#         (name, steps, result_A, result_B) = result
#         print_r(name + '_A', steps, result_A, colors.lYLW)
#         print_r(name + '_B', steps, result_B, colors.lYLW)
