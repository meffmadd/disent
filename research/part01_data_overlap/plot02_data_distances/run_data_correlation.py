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


from typing import Optional
from typing import Tuple
from typing import Union

import numpy as np
import torch
from scipy.stats import spearmanr, pearsonr

from disent.nn.loss.reduction import batch_loss_reduction
from tqdm import tqdm
import torch.nn.functional as F

from disent.dataset import DisentDataset
from disent.dataset.data import Cars3d64Data
from disent.dataset.data import DSpritesData
from disent.dataset.data import Shapes3dData
from disent.dataset.data import SmallNorb64Data
from research.code.dataset.data import XYSquaresData
from disent.dataset.transform import ToImgTensorF32
from disent.util.function import wrapped_partial


# ========================================================================= #
# plot                                                                      #
# ========================================================================= #



# NOTE: this should match _factored_components._dists_compute_scores!
#       -- this is taken directly from there!
from research.code.metrics._factored_components import _unswapped_ratio
from research.code.metrics._factored_components import _unswapped_ratio_numpy


def _compute_rcorr_ground_data(num_samples: int, xs_traversal: torch.Tensor, factors: torch.Tensor):
    # checks
    assert len(factors) == len(xs_traversal)
    assert factors.device == xs_traversal.device
    # ------------------------ #
    # generate random triplets
    # - {p, n} indices do not need to be sorted like triplets, these can be random.
    #   This metric is symmetric for swapped p & n values.
    idxs_a, idxs_p, idxs_n = torch.randint(0, len(xs_traversal), size=(3, num_samples), device=xs_traversal.device)
    # compute distances -- shape: (num,)
    ap_ground_dists: np.ndarray = torch.norm(factors[idxs_a, :] - factors[idxs_p, :], p=1, dim=-1).numpy()
    an_ground_dists: np.ndarray = torch.norm(factors[idxs_a, :] - factors[idxs_n, :], p=1, dim=-1).numpy()
    ap_data_dists: np.ndarray = batch_loss_reduction(F.mse_loss(xs_traversal[idxs_a, ...], xs_traversal[idxs_p, ...], reduction='none'), reduction_dtype=torch.float32, reduction='mean').numpy()
    an_data_dists: np.ndarray = batch_loss_reduction(F.mse_loss(xs_traversal[idxs_a, ...], xs_traversal[idxs_n, ...], reduction='none'), reduction_dtype=torch.float32, reduction='mean').numpy()
    # ------------------------ #
    # concatenate values -- shape: (2 * num,)
    # ground_dists = np.concatenate([ap_ground_dists, an_ground_dists], axis=0)
    # data_dists   = np.concatenate([ap_data_dists,   an_data_dists],   axis=0)
    # ------------------------ #
    # compute rcorr scores -- shape: ()
    # - check the number of swapped elements along a factor for random triplets.
    rsame_ground_data = _unswapped_ratio_numpy(ap0=ap_ground_dists, an0=an_ground_dists, ap1=ap_data_dists, an1=an_data_dists)
    # - compute the pearson rank correlation coefficient over the concatenated distances -- TODO: this is wrong! we actually need to compute this over distance batches!
    linear_corr, _ = pearsonr(np.concatenate([ap_ground_dists, an_ground_dists], axis=0), np.concatenate([ap_data_dists, an_data_dists], axis=0))
    rank_corr, _ = spearmanr(np.concatenate([ap_ground_dists, an_ground_dists], axis=0), np.concatenate([ap_data_dists, an_data_dists], axis=0))
    # ------------------------ #
    # return values -- shape: ()
    return ((ap_ground_dists, an_ground_dists), (ap_data_dists, an_data_dists)), (linear_corr, rank_corr, rsame_ground_data)


@torch.no_grad()
def _compute_mean_rcorr_ground_data(dataset: DisentDataset, f_idx: Optional[Union[str, int]], num_samples: int, repeats: int, progress: bool = True, random_batch_size: Optional[int] = 64):
    # normalise everything!
    if f_idx in ('random', None):
        f_idx = None
        f_name = 'random'
    elif isinstance(f_idx, str):
        f_idx = dataset.gt_data.normalise_factor_idx(f_idx)
        f_name = dataset.gt_data.factor_names[f_idx]
    else:
        assert isinstance(f_idx, int)
        f_name = dataset.gt_data.factor_names[f_idx]
    # get defaults
    if random_batch_size is None:
        random_batch_size = int(np.mean(dataset.gt_data.factor_sizes))
    # compute averages
    correlations_linear = []
    correlations_rank = []
    rsame_ratios = []
    all_ap_ground_dists, all_an_ground_dists = [], []
    all_ap_data_dists, all_an_data_dists = [], []
    # repeat!
    for i in tqdm(range(repeats), desc=f'{dataset.gt_data.name}: {f_name}', disable=not progress):
        # sample random factors
        if f_idx is None:
            factors = dataset.gt_data.sample_factors(size=random_batch_size)
        else:
            factors = dataset.gt_data.sample_random_factor_traversal(f_idx=f_idx)
        # encode factors
        xs = dataset.dataset_batch_from_factors(factors, 'input').cpu()
        factors = torch.from_numpy(factors).to(torch.float32).cpu()
        # [COMPUTE SAME RATIO & CORRELATION]
        ((ap_ground_dists, an_ground_dists), (ap_data_dists, an_data_dists)), (linear_corr, rank_corr, rsame_ground_data) = _compute_rcorr_ground_data(num_samples, xs_traversal=xs, factors=factors)
        # [UPDATE SCORES]
        correlations_linear.append(linear_corr)
        correlations_rank.append(rank_corr)
        rsame_ratios.append(rsame_ground_data)
        all_ap_ground_dists.append(ap_ground_dists)
        all_an_ground_dists.append(an_ground_dists)
        all_ap_data_dists.append(ap_data_dists)
        all_an_data_dists.append(an_data_dists)
    # combine
    correlations_linear = np.array(correlations_linear)
    correlations_rank   = np.array(correlations_rank)
    all_ap_ground_dists = np.concatenate(all_ap_ground_dists, axis=0)
    all_an_ground_dists = np.concatenate(all_an_ground_dists, axis=0)
    all_ap_data_dists   = np.concatenate(all_ap_data_dists, axis=0)
    all_an_data_dists   = np.concatenate(all_an_data_dists, axis=0)
    # compute the mean scores
    mean_linear_corr = np.mean(correlations_linear)
    mean_rank_corr   = np.mean(correlations_rank)
    mean_rsame_ratio = np.mean(rsame_ratios)
    # compute the scores
    rsame_ratio = _unswapped_ratio_numpy(ap0=all_ap_ground_dists, an0=all_an_ground_dists, ap1=all_ap_data_dists, an1=all_an_data_dists)
    linear_corr, _ = pearsonr(np.concatenate([all_ap_ground_dists, all_an_ground_dists], axis=0), np.concatenate([all_ap_data_dists, all_an_data_dists], axis=0))
    rank_corr, _ = spearmanr(np.concatenate([all_ap_ground_dists, all_an_ground_dists], axis=0), np.concatenate([all_ap_data_dists, all_an_data_dists], axis=0))
    # done!
    return {
        'linear_corr': linear_corr,
        'mean_linear_corr': mean_linear_corr,
        'rank_corr': rank_corr,
        'mean_rank_corr': mean_rank_corr,
        'rsame_ratio': rsame_ratio,
        'mean_rsame_ratio': mean_rsame_ratio,
    }

# ========================================================================= #
# entrypoint                                                                #
# ========================================================================= #


if __name__ == '__main__':

    def main():
        gt_data_classes = {
          # 'XYObject':  wrapped_partial(XYObjectData),
          # 'XYBlocks':  wrapped_partial(XYBlocksData),
          #   'XYSquares': wrapped_partial(XYSquaresData),
            'DSprites':  wrapped_partial(DSpritesData),
            'Shapes3d':  wrapped_partial(Shapes3dData),
            'Cars3d':    wrapped_partial(Cars3d64Data),
            'SmallNorb': wrapped_partial(SmallNorb64Data),
          # 'Mpi3d':     wrapped_partial(Mpi3dData),

            'XYSquares-1-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=1, grid_size=8, no_warnings=True),
            'XYSquares-2-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=2, grid_size=8, no_warnings=True),
            'XYSquares-3-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=3, grid_size=8, no_warnings=True),
            'XYSquares-4-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=4, grid_size=8, no_warnings=True),
            'XYSquares-5-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=5, grid_size=8, no_warnings=True),
            'XYSquares-6-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=6, grid_size=8, no_warnings=True),
            'XYSquares-7-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=7, grid_size=8, no_warnings=True),
            'XYSquares-8-8': wrapped_partial(XYSquaresData, square_size=8, grid_spacing=8, grid_size=8, no_warnings=True),
        }

        num_samples = 64
        repeats = 1024
        progress = False

        for name, data_cls in  gt_data_classes.items():
            dataset = DisentDataset(data_cls(), transform=ToImgTensorF32(size=64))
            factor_names = (*dataset.gt_data.factor_names, 'random')
            # compute over each factor name
            for f_name in [*dataset.gt_data.factor_names, 'random']:
                f_size = dataset.gt_data.factor_sizes[dataset.gt_data.normalise_factor_idx(f_name)] if f_name != 'random' else len(dataset)
                try:
                    scores = _compute_mean_rcorr_ground_data(dataset, f_idx=f_name, num_samples=num_samples, repeats=repeats, progress=progress)
                    print(f'[{name}] f_idx={f_name:{max(len(s) for s in factor_names)}s} f_size={f_size} {" ".join(f"{k}={v:7.5f}" for k, v in scores.items())}')
                except Exception as e:
                    print(f'[{name}] f_idx={f_name:{max(len(s) for s in factor_names)}s} f_size={f_size} SKIPPED!')
                    raise e
            print()

    main()


# ========================================================================= #
# END                                                                       #
# ========================================================================= #
