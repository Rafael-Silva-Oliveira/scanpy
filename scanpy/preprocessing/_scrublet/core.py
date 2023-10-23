from __future__ import annotations

import time
from typing import cast

import numpy as np
from scipy import sparse
from numpy.typing import NDArray

from ..._utils import AnyRandom
from .utils import (
    get_knn_graph,
    pipeline_apply_gene_filter,
    pipeline_get_gene_filter,
    pipeline_log_transform,
    pipeline_mean_center,
    pipeline_normalize,
    pipeline_normalize_variance,
    pipeline_pca,
    pipeline_truncated_svd,
    pipeline_zscore,
    print_optional,
    subsample_counts,
)


class Scrublet:
    def __init__(
        self,
        counts_matrix: sparse.spmatrix | NDArray[np.integer],
        *,
        total_counts: NDArray[np.integer] | None = None,
        sim_doublet_ratio: float = 2.0,
        n_neighbors: int | None = None,
        expected_doublet_rate: float = 0.1,
        stdev_doublet_rate: float = 0.02,
        random_state: AnyRandom = 0,
    ) -> None:
        """\
        Initialize Scrublet object with counts matrix and doublet prediction parameters

        Parameters
        ----------
        counts_matrix
            Matrix with shape (n_cells, n_genes) containing raw (unnormalized)
            UMI-based transcript counts.
            Converted into a :class:`scipy.sparse.csc_matrix`.

        total_counts
            Array with shape (n_cells,) of total UMI counts per cell.
            If `None`, this is calculated as the row sums of `counts_matrix`.

        sim_doublet_ratio
            Number of doublets to simulate relative to the number of observed
            transcriptomes.

        n_neighbors
            Number of neighbors used to construct the KNN graph of observed
            transcriptomes and simulated doublets.
            If `None`, this is set to round(0.5 * sqrt(n_cells))

        expected_doublet_rate
            The estimated doublet rate for the experiment.

        stdev_doublet_rate
            Uncertainty in the expected doublet rate.

        random_state
            Random state for doublet simulation, approximate
            nearest neighbor search, and PCA/TruncatedSVD.

        Attributes
        ----------
        predicted_doublets_ : ndarray, shape (n_cells,)
            Boolean mask of predicted doublets in the observed transcriptomes.

        doublet_scores_obs_ : ndarray, shape (n_cells,)
            Doublet scores for observed transcriptomes.

        doublet_scores_sim_ : ndarray, shape (n_doublets,)
            Doublet scores for simulated doublets.

        doublet_errors_obs_ : ndarray, shape (n_cells,)
            Standard error in the doublet scores for observed transcriptomes.

        doublet_errors_sim_ : ndarray, shape (n_doublets,)
            Standard error in the doublet scores for simulated doublets.

        threshold_: float
            Doublet score threshold for calling a transcriptome
            a doublet.

        z_scores_ : ndarray, shape (n_cells,)
            Z-score conveying confidence in doublet calls.
            Z = `(doublet_score_obs_ - threhsold_) / doublet_errors_obs_`

        detected_doublet_rate_: float
            Fraction of observed transcriptomes that have been called doublets.

        detectable_doublet_fraction_: float
            Estimated fraction of doublets that are detectable, i.e.,
            fraction of simulated doublets with doublet scores above `threshold_`

        overall_doublet_rate_: float
            Estimated overall doublet rate,
            `detected_doublet_rate_ / detectable_doublet_fraction_`.
            Should agree (roughly) with `expected_doublet_rate`.

        manifold_obs_: ndarray, shape (n_cells, n_features)
            The single-cell "manifold" coordinates (e.g., PCA coordinates)
            for observed transcriptomes. Nearest neighbors are found using
            the union of `manifold_obs_` and `manifold_sim_` (see below).

        manifold_sim_: ndarray, shape (n_doublets, n_features)
            The single-cell "manifold" coordinates (e.g., PCA coordinates)
            for simulated doublets. Nearest neighbors are found using
            the union of `manifold_obs_` (see above) and `manifold_sim_`.

        doublet_parents_ : ndarray, shape (n_doublets, 2)
            Indices of the observed transcriptomes used to generate the
            simulated doublets.

        doublet_neighbor_parents_ : list, length n_cells
            A list of arrays of the indices of the doublet neighbors of
            each observed transcriptome (the ith entry is an array of
            the doublet neighbors of transcriptome i).
        """

        if not isinstance(counts_matrix, sparse.csc_matrix):
            counts_matrix = sparse.csc_matrix(counts_matrix)

        # initialize counts matrices
        self._E_obs = counts_matrix
        self._E_sim = None
        self._E_obs_norm = None
        self._E_sim_norm = None

        if total_counts is None:
            self._total_counts_obs = self._E_obs.sum(1).A.squeeze()
        else:
            self._total_counts_obs = total_counts

        self._gene_filter = np.arange(self._E_obs.shape[1])
        self._embeddings = {}

        self.sim_doublet_ratio = sim_doublet_ratio
        self.n_neighbors = n_neighbors
        self.expected_doublet_rate = expected_doublet_rate
        self.stdev_doublet_rate = stdev_doublet_rate
        self.random_state = random_state

        if self.n_neighbors is None:
            self.n_neighbors = int(round(0.5 * np.sqrt(self._E_obs.shape[0])))

    ######## Core Scrublet functions ########

    def scrub_doublets(
        self,
        *,
        synthetic_doublet_umi_subsampling: float = 1.0,
        use_approx_neighbors: bool = True,
        distance_metric: str = 'euclidean',
        get_doublet_neighbor_parents: bool = False,
        min_counts: int = 3,
        min_cells: int = 3,
        min_gene_variability_pctl: float | int = 85,
        log_transform: bool = False,
        mean_center: bool = True,
        normalize_variance: bool = True,
        n_prin_comps: int = 30,
        svd_solver: str = 'arpack',
        verbose: bool = True,
    ) -> tuple[NDArray[np.float64], NDArray[np.bool_] | None]:
        """Standard pipeline for preprocessing, doublet simulation, and doublet prediction

        Automatically sets a threshold for calling doublets, but it's best to check
        this by running plot_histogram() afterwards and adjusting threshold
        with call_doublets(threshold=new_threshold) if necessary.

        Arguments
        ---------
        synthetic_doublet_umi_subsampling : float, optional (defuault: 1.0)
            Rate for sampling UMIs when creating synthetic doublets. If 1.0,
            each doublet is created by simply adding the UMIs from two randomly
            sampled observed transcriptomes. For values less than 1, the
            UMI counts are added and then randomly sampled at the specified
            rate.

        use_approx_neighbors : bool, optional (default: True)
            Use approximate nearest neighbor method (annoy) for the KNN
            classifier.

        distance_metric : str, optional (default: 'euclidean')
            Distance metric used when finding nearest neighbors. For list of
            valid values, see the documentation for annoy (if `use_approx_neighbors`
            is True) or sklearn.neighbors.NearestNeighbors (if `use_approx_neighbors`
            is False).

        get_doublet_neighbor_parents : bool, optional (default: False)
            If True, return the parent transcriptomes that generated the
            doublet neighbors of each observed transcriptome. This information can
            be used to infer the cell states that generated a given
            doublet state.

        min_counts : float, optional (default: 3)
            Used for gene filtering prior to PCA. Genes expressed at fewer than
            `min_counts` in fewer than `min_cells` (see below) are excluded.

        min_cells : int, optional (default: 3)
            Used for gene filtering prior to PCA. Genes expressed at fewer than
            `min_counts` (see above) in fewer than `min_cells` are excluded.

        min_gene_variability_pctl : float, optional (default: 85.0)
            Used for gene filtering prior to PCA. Keep the most highly variable genes
            (in the top min_gene_variability_pctl percentile), as measured by
            the v-statistic [Klein et al., Cell 2015].

        log_transform : bool, optional (default: False)
            If True, log-transform the counts matrix (log10(1+TPM)).
            `sklearn.decomposition.TruncatedSVD` will be used for dimensionality
            reduction, unless `mean_center` is True.

        mean_center : bool, optional (default: True)
            If True, center the data such that each gene has a mean of 0.
            `sklearn.decomposition.PCA` will be used for dimensionality
            reduction.

        normalize_variance : bool, optional (default: True)
            If True, normalize the data such that each gene has a variance of 1.
            `sklearn.decomposition.TruncatedSVD` will be used for dimensionality
            reduction, unless `mean_center` is True.

        n_prin_comps : int, optional (default: 30)
            Number of principal components used to embed the transcriptomes prior
            to k-nearest-neighbor graph construction.

        svd_solver : str, optional (default: 'arpack')
            SVD solver to use. See available options for
            `svd_solver` from `sklearn.decomposition.PCA` or
            `algorithm` from `sklearn.decomposition.TruncatedSVD`

        verbose : bool, optional (default: True)
            If True, print progress updates.

        Sets
        ----
        doublet_scores_obs_, doublet_errors_obs_,
        doublet_scores_sim_, doublet_errors_sim_,
        predicted_doublets_, z_scores_
        threshold_, detected_doublet_rate_,
        detectable_doublet_fraction_, overall_doublet_rate_,
        doublet_parents_, doublet_neighbor_parents_
        """
        t0 = time.time()

        self._E_sim = None
        self._E_obs_norm = None
        self._E_sim_norm = None
        self._gene_filter = np.arange(self._E_obs.shape[1])

        print_optional('Preprocessing...', verbose)
        pipeline_normalize(self)
        pipeline_get_gene_filter(
            self,
            min_counts=min_counts,
            min_cells=min_cells,
            min_gene_variability_pctl=min_gene_variability_pctl,
        )
        pipeline_apply_gene_filter(self)

        print_optional('Simulating doublets...', verbose)
        self.simulate_doublets(
            sim_doublet_ratio=self.sim_doublet_ratio,
            synthetic_doublet_umi_subsampling=synthetic_doublet_umi_subsampling,
        )
        pipeline_normalize(self, postnorm_total=1e6)
        if log_transform:
            pipeline_log_transform(self)
        if mean_center and normalize_variance:
            pipeline_zscore(self)
        elif mean_center:
            pipeline_mean_center(self)
        elif normalize_variance:
            pipeline_normalize_variance(self)

        if mean_center:
            print_optional('Embedding transcriptomes using PCA...', verbose)
            pipeline_pca(
                self,
                n_prin_comps=n_prin_comps,
                random_state=self.random_state,
                svd_solver=svd_solver,
            )
        else:
            print_optional('Embedding transcriptomes using Truncated SVD...', verbose)
            pipeline_truncated_svd(
                self,
                n_prin_comps=n_prin_comps,
                random_state=self.random_state,
                algorithm=svd_solver,
            )

        print_optional('Calculating doublet scores...', verbose)
        self.calculate_doublet_scores(
            use_approx_neighbors=use_approx_neighbors,
            distance_metric=distance_metric,
            get_doublet_neighbor_parents=get_doublet_neighbor_parents,
        )
        self.call_doublets(verbose=verbose)

        t1 = time.time()
        print_optional('Elapsed time: {:.1f} seconds'.format(t1 - t0), verbose)
        return self.doublet_scores_obs_, self.predicted_doublets_

    def simulate_doublets(
        self,
        *,
        sim_doublet_ratio: float | None = None,
        synthetic_doublet_umi_subsampling: float = 1.0,
    ) -> None:
        """Simulate doublets by adding the counts of random observed transcriptome pairs.

        Arguments
        ---------
        sim_doublet_ratio : float, optional (default: None)
            Number of doublets to simulate relative to the number of observed
            transcriptomes. If `None`, self.sim_doublet_ratio is used.

        synthetic_doublet_umi_subsampling : float, optional (defuault: 1.0)
            Rate for sampling UMIs when creating synthetic doublets. If 1.0,
            each doublet is created by simply adding the UMIs from two randomly
            sampled observed transcriptomes. For values less than 1, the
            UMI counts are added and then randomly sampled at the specified
            rate.

        Sets
        ----
        doublet_parents_
        """

        if sim_doublet_ratio is None:
            sim_doublet_ratio = self.sim_doublet_ratio
        else:
            self.sim_doublet_ratio = sim_doublet_ratio

        n_obs = self._E_obs.shape[0]
        n_sim = int(n_obs * sim_doublet_ratio)

        np.random.seed(self.random_state)
        pair_ix = np.random.randint(0, n_obs, size=(n_sim, 2))

        E1 = self._E_obs[pair_ix[:, 0], :]
        E2 = self._E_obs[pair_ix[:, 1], :]
        tots1 = self._total_counts_obs[pair_ix[:, 0]]
        tots2 = self._total_counts_obs[pair_ix[:, 1]]
        if synthetic_doublet_umi_subsampling < 1:
            self._E_sim, self._total_counts_sim = subsample_counts(
                E1 + E2,
                synthetic_doublet_umi_subsampling,
                tots1 + tots2,
                random_seed=self.random_state,
            )
        else:
            self._E_sim = E1 + E2
            self._total_counts_sim = tots1 + tots2
        self.doublet_parents_ = pair_ix
        return

    def set_manifold(
        self, manifold_obs: NDArray[np.integer], manifold_sim: NDArray[np.integer]
    ) -> None:
        """\
        Set the manifold coordinates used in k-nearest-neighbor graph construction

        Arguments
        ---------
        manifold_obs: ndarray, shape (n_cells, n_features)
            The single-cell "manifold" coordinates (e.g., PCA coordinates)
            for observed transcriptomes. Nearest neighbors are found using
            the union of `manifold_obs` and `manifold_sim` (see below).

        manifold_sim: ndarray, shape (n_doublets, n_features)
            The single-cell "manifold" coordinates (e.g., PCA coordinates)
            for simulated doublets. Nearest neighbors are found using
            the union of `manifold_obs` (see above) and `manifold_sim`.

        Sets
        ----
        manifold_obs_, manifold_sim_,
        """

        self.manifold_obs_ = manifold_obs
        self.manifold_sim_ = manifold_sim

    def calculate_doublet_scores(
        self,
        use_approx_neighbors: bool = True,
        distance_metric: str = 'euclidean',
        get_doublet_neighbor_parents: bool = False,
    ) -> NDArray[np.float64]:
        """\
        Calculate doublet scores for observed transcriptomes and simulated doublets

        Requires that manifold_obs_ and manifold_sim_ have already been set.

        Arguments
        ---------
        use_approx_neighbors : bool, optional (default: True)
            Use approximate nearest neighbor method (annoy) for the KNN
            classifier.

        distance_metric : str, optional (default: 'euclidean')
            Distance metric used when finding nearest neighbors. For list of
            valid values, see the documentation for annoy (if `use_approx_neighbors`
            is True) or sklearn.neighbors.NearestNeighbors (if `use_approx_neighbors`
            is False).

        get_doublet_neighbor_parents : bool, optional (default: False)
            If True, return the parent transcriptomes that generated the
            doublet neighbors of each observed transcriptome. This information can
            be used to infer the cell states that generated a given
            doublet state.

        Sets
        ----
        doublet_scores_obs_, doublet_scores_sim_,
        doublet_errors_obs_, doublet_errors_sim_,
        doublet_neighbor_parents_
        """

        self._nearest_neighbor_classifier(
            k=self.n_neighbors,
            exp_doub_rate=self.expected_doublet_rate,
            stdev_doub_rate=self.stdev_doublet_rate,
            use_approx_nn=use_approx_neighbors,
            distance_metric=distance_metric,
            get_neighbor_parents=get_doublet_neighbor_parents,
        )
        return self.doublet_scores_obs_

    def _nearest_neighbor_classifier(
        self,
        k: int = 40,
        *,
        use_approx_nn: bool = True,
        distance_metric: str = 'euclidean',
        exp_doub_rate: float = 0.1,
        stdev_doub_rate: float = 0.03,
        get_neighbor_parents: bool = False,
    ) -> None:
        manifold = np.vstack((self.manifold_obs_, self.manifold_sim_))
        doub_labels = np.concatenate(
            (
                np.zeros(self.manifold_obs_.shape[0], dtype=np.int64),
                np.ones(self.manifold_sim_.shape[0], dtype=np.int64),
            )
        )

        n_obs: int = (doub_labels == 0).sum()
        n_sim: int = (doub_labels == 1).sum()

        # Adjust k (number of nearest neighbors) based on the ratio of simulated to observed cells
        k_adj = int(round(k * (1 + n_sim / float(n_obs))))

        # Find k_adj nearest neighbors
        neighbors = get_knn_graph(
            manifold,
            k=k_adj,
            dist_metric=distance_metric,
            approx=use_approx_nn,
            return_edges=False,
            random_seed=self.random_state,
        )

        # Calculate doublet score based on ratio of simulated cell neighbors vs. observed cell neighbors
        doub_neigh_mask: NDArray[np.bool_] = doub_labels[neighbors] == 1
        n_sim_neigh: NDArray[np.int64] = doub_neigh_mask.sum(1)

        rho = exp_doub_rate
        r = n_sim / float(n_obs)
        nd = n_sim_neigh.astype(np.float64)
        N = float(k_adj)

        # Bayesian
        q = (nd + 1) / (N + 2)
        Ld = q * rho / r / (1 - rho - q * (1 - rho - rho / r))

        se_q = np.sqrt(q * (1 - q) / (N + 3))
        se_rho = stdev_doub_rate

        se_Ld = (
            q
            * rho
            / r
            / (1 - rho - q * (1 - rho - rho / r)) ** 2
            * np.sqrt((se_q / q * (1 - rho)) ** 2 + (se_rho / rho * (1 - q)) ** 2)
        )

        self.doublet_scores_obs_ = Ld[doub_labels == 0]
        self.doublet_scores_sim_ = Ld[doub_labels == 1]
        self.doublet_errors_obs_ = se_Ld[doub_labels == 0]
        self.doublet_errors_sim_ = se_Ld[doub_labels == 1]

        # get parents of doublet neighbors, if requested
        neighbor_parents = None
        if get_neighbor_parents:
            parent_cells = self.doublet_parents_
            neighbors = neighbors - n_obs
            neighbor_parents = []
            for iCell in range(n_obs):
                this_doub_neigh = neighbors[iCell, :][neighbors[iCell, :] > -1]
                if len(this_doub_neigh) > 0:
                    this_doub_neigh_parents = np.unique(
                        parent_cells[this_doub_neigh, :].flatten()
                    )
                    neighbor_parents.append(this_doub_neigh_parents)
                else:
                    neighbor_parents.append([])
            self.doublet_neighbor_parents_ = np.array(neighbor_parents)

    def call_doublets(
        self, *, threshold: float | None = None, verbose: bool = True
    ) -> NDArray[np.bool_] | None:
        """\
        Call trancriptomes as doublets or singlets

        Arguments
        ---------
        threshold : float, optional (default: None)
            Doublet score threshold for calling a transcriptome
            a doublet. If `None`, this is set automatically by looking
            for the minimum between the two modes of the `doublet_scores_sim_`
            histogram. It is best practice to check the threshold visually
            using the `doublet_scores_sim_` histogram and/or based on
            co-localization of predicted doublets in a 2-D embedding.

        verbose : bool, optional (default: True)
            If True, print summary statistics.

        Sets
        ----
        predicted_doublets_, z_scores_, threshold_,
        detected_doublet_rate_, detectable_doublet_fraction,
        overall_doublet_rate_
        """

        if threshold is None:
            # automatic threshold detection
            # http://scikit-image.org/docs/dev/api/skimage.filters.html
            from skimage.filters import threshold_minimum

            try:
                threshold = cast(float, threshold_minimum(self.doublet_scores_sim_))
                if verbose:
                    print(
                        "Automatically set threshold at doublet score = {:.2f}".format(
                            threshold
                        )
                    )
            except Exception:
                self.predicted_doublets_ = None
                if verbose:
                    print(
                        "Warning: failed to automatically identify doublet score threshold. Run `call_doublets` with user-specified threshold."
                    )
                return self.predicted_doublets_

        Ld_obs = self.doublet_scores_obs_
        Ld_sim = self.doublet_scores_sim_
        se_obs = self.doublet_errors_obs_
        Z = (Ld_obs - threshold) / se_obs
        self.predicted_doublets_ = Ld_obs > threshold
        self.z_scores_ = Z
        self.threshold_ = threshold
        self.detected_doublet_rate_: float = (Ld_obs > threshold).sum() / float(
            len(Ld_obs)
        )
        self.detectable_doublet_fraction_: float = (Ld_sim > threshold).sum() / float(
            len(Ld_sim)
        )
        self.overall_doublet_rate_ = (
            self.detected_doublet_rate_ / self.detectable_doublet_fraction_
        )

        if verbose:
            print(
                'Detected doublet rate = {:.1f}%'.format(
                    100 * self.detected_doublet_rate_
                )
            )
            print(
                'Estimated detectable doublet fraction = {:.1f}%'.format(
                    100 * self.detectable_doublet_fraction_
                )
            )
            print('Overall doublet rate:')
            print('\tExpected   = {:.1f}%'.format(100 * self.expected_doublet_rate))
            print('\tEstimated  = {:.1f}%'.format(100 * self.overall_doublet_rate_))

        return self.predicted_doublets_
