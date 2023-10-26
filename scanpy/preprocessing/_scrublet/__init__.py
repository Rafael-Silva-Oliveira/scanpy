from __future__ import annotations

import numpy as np
import pandas as pd
from anndata import AnnData
from scipy import sparse

from ... import logging as logg
from ... import preprocessing as pp
from ...get import _get_obs_rep
from ..._utils import AnyRandom
from . import pipeline
from .core import Scrublet
from .neighbors import AnnoyDist


def scrublet(
    adata: AnnData,
    adata_sim: AnnData | None = None,
    *,
    batch_key: str | None = None,
    sim_doublet_ratio: float = 2.0,
    expected_doublet_rate: float = 0.05,
    stdev_doublet_rate: float = 0.02,
    synthetic_doublet_umi_subsampling: float = 1.0,
    knn_dist_metric: AnnoyDist = "euclidean",
    normalize_variance: bool = True,
    log_transform: bool = False,
    mean_center: bool = True,
    n_prin_comps: int = 30,
    use_approx_neighbors: bool = True,
    get_doublet_neighbor_parents: bool = False,
    n_neighbors: int | None = None,
    threshold: float | None = None,
    verbose: bool = True,
    copy: bool = False,
    random_state: AnyRandom = 0,
) -> AnnData | None:
    """\
    Predict doublets using Scrublet [Wolock19]_.

    Predict cell doublets using a nearest-neighbor classifier of observed
    transcriptomes and simulated doublets. Works best if the input is a raw
    (unnormalized) counts matrix from a single sample or a collection of
    similar samples from the same experiment.
    This function is a wrapper around functions that pre-process using Scanpy
    and directly call functions of Scrublet(). You may also undertake your own
    preprocessing, simulate doublets with
    :func:`~scanpy.pp.scrublet_simulate_doublets`, and run the core scrublet
    function :func:`~scanpy.pp.scrublet`.

    .. note::
        More information and bug reports `here
        <https://github.com/swolock/scrublet>`__.

    Parameters
    ----------
    adata
        The annotated data matrix of shape ``n_obs`` × ``n_vars``. Rows
        correspond to cells and columns to genes. Expected to be un-normalised
        where adata_sim is not supplied, in which case doublets will be
        simulated and pre-processing applied to both objects. If adata_sim is
        supplied, this should be the observed transcriptomes processed
        consistently (filtering, transform, normalisaton, hvg) with adata_sim.
    adata_sim
        (Advanced use case) Optional annData object generated by
        :func:`~scanpy.pp.scrublet_simulate_doublets`, with same number of vars
        as adata. This should have been built from adata_obs after
        filtering genes and cells and selcting highly-variable genes.
    batch_key
        Optional :attr:`~anndata.AnnData.obs` column name discriminating between batches.
    sim_doublet_ratio
        Number of doublets to simulate relative to the number of observed
        transcriptomes.
    expected_doublet_rate
        Where adata_sim not suplied, the estimated doublet rate for the
        experiment.
    stdev_doublet_rate
        Where adata_sim not suplied, uncertainty in the expected doublet rate.
    synthetic_doublet_umi_subsampling
        Where adata_sim not suplied, rate for sampling UMIs when creating
        synthetic doublets. If 1.0, each doublet is created by simply adding
        the UMI counts from two randomly sampled observed transcriptomes. For
        values less than 1, the UMI counts are added and then randomly sampled
        at the specified rate.
    knn_dist_metric
        Distance metric used when finding nearest neighbors. For list of
        valid values, see the documentation for annoy (if `use_approx_neighbors`
        is True) or sklearn.neighbors.NearestNeighbors (if `use_approx_neighbors`
        is False).
    normalize_variance
        If True, normalize the data such that each gene has a variance of 1.
        :class:`sklearn.decomposition.TruncatedSVD` will be used for dimensionality
        reduction, unless `mean_center` is True.
    log_transform
        Whether to use :func:`~scanpy.pp.log1p` to log-transform the data
        prior to PCA.
    mean_center
        If True, center the data such that each gene has a mean of 0.
        :class:`sklearn.decomposition.PCA` will be used for dimensionality
        reduction.
    n_prin_comps
        Number of principal components used to embed the transcriptomes prior
        to k-nearest-neighbor graph construction.
    use_approx_neighbors
        Use approximate nearest neighbor method (annoy) for the KNN
        classifier.
    get_doublet_neighbor_parents
        If True, return (in .uns) the parent transcriptomes that generated the
        doublet neighbors of each observed transcriptome. This information can
        be used to infer the cell states that generated a given doublet state.
    n_neighbors
        Number of neighbors used to construct the KNN graph of observed
        transcriptomes and simulated doublets. If ``None``, this is
        automatically set to ``np.round(0.5 * np.sqrt(n_obs))``.
    threshold
        Doublet score threshold for calling a transcriptome a doublet. If
        `None`, this is set automatically by looking for the minimum between
        the two modes of the `doublet_scores_sim_` histogram. It is best
        practice to check the threshold visually using the
        `doublet_scores_sim_` histogram and/or based on co-localization of
        predicted doublets in a 2-D embedding.
    verbose
        If True, log progress updates.
    copy
        If :data:`True`, return a copy of the input ``adata`` with Scrublet results
        added. Otherwise, Scrublet results are added in place.
    random_state
        Initial state for doublet simulation and nearest neighbors.

    Returns
    -------
    adata : anndata.AnnData
        if ``copy=True`` it returns or else adds fields to ``adata``. Those fields:

        ``.obs['doublet_score']``
            Doublet scores for each observed transcriptome

        ``.obs['predicted_doublet']``
            Boolean indicating predicted doublet status

        ``.uns['scrublet']['doublet_scores_sim']``
            Doublet scores for each simulated doublet transcriptome

        ``.uns['scrublet']['doublet_parents']``
            Pairs of ``.obs_names`` used to generate each simulated doublet
            transcriptome

        ``.uns['scrublet']['parameters']``
            Dictionary of Scrublet parameters

    See also
    --------
    :func:`~scanpy.pp.scrublet_simulate_doublets`: Run Scrublet's doublet
        simulation separately for advanced usage.
    :func:`~scanpy.pl.scrublet_score_distribution`: Plot histogram of doublet
        scores for observed transcriptomes and simulated doublets.
    """

    if copy:
        adata = adata.copy()

    start = logg.info("Running Scrublet")

    adata_obs = adata.copy()

    def _run_scrublet(ad_obs, ad_sim=None):
        # With no adata_sim we assume the regular use case, starting with raw
        # counts and simulating doublets

        if ad_sim is None:
            pp.filter_genes(ad_obs, min_cells=3)
            pp.filter_cells(ad_obs, min_genes=3)

            # Doublet simulation will be based on the un-normalised counts, but on the
            # selection of genes following normalisation and variability filtering. So
            # we need to save the raw and subset at the same time.

            ad_obs.layers["raw"] = ad_obs.X.copy()
            pp.normalize_total(ad_obs)

            # HVG process needs log'd data.

            logged = pp.log1p(ad_obs, copy=True)
            pp.highly_variable_genes(logged)
            ad_obs = ad_obs[:, logged.var["highly_variable"]].copy()

            # Simulate the doublets based on the raw expressions from the normalised
            # and filtered object.

            ad_sim = scrublet_simulate_doublets(
                ad_obs,
                layer="raw",
                sim_doublet_ratio=sim_doublet_ratio,
                synthetic_doublet_umi_subsampling=synthetic_doublet_umi_subsampling,
            )

            if log_transform:
                pp.log1p(ad_obs)
                pp.log1p(ad_sim)

            # Now normalise simulated and observed in the same way

            pp.normalize_total(ad_obs, target_sum=1e6)
            pp.normalize_total(ad_sim, target_sum=1e6)

        ad_obs = _scrublet_call_doublets(
            adata_obs=ad_obs,
            adata_sim=ad_sim,
            n_neighbors=n_neighbors,
            expected_doublet_rate=expected_doublet_rate,
            stdev_doublet_rate=stdev_doublet_rate,
            mean_center=mean_center,
            normalize_variance=normalize_variance,
            n_prin_comps=n_prin_comps,
            use_approx_neighbors=use_approx_neighbors,
            knn_dist_metric=knn_dist_metric,
            get_doublet_neighbor_parents=get_doublet_neighbor_parents,
            threshold=threshold,
            random_state=random_state,
            verbose=verbose,
        )

        return {"obs": ad_obs.obs, "uns": ad_obs.uns["scrublet"]}

    if batch_key is not None:
        if batch_key not in adata.obs.keys():
            raise ValueError(
                "`batch_key` must be a column of .obs in the input annData object."
            )

        # Run Scrublet independently on batches and return just the
        # scrublet-relevant parts of the objects to add to the input object

        batches = np.unique(adata.obs[batch_key])
        scrubbed = [
            _run_scrublet(
                adata_obs[adata_obs.obs[batch_key] == batch].copy(),
                adata_sim,
            )
            for batch in batches
        ]
        scrubbed_obs = pd.concat([scrub["obs"] for scrub in scrubbed])

        # Now reset the obs to get the scrublet scores

        adata.obs = scrubbed_obs.loc[adata.obs_names.values]

        # Save the .uns from each batch separately

        adata.uns["scrublet"] = {}
        adata.uns["scrublet"]["batches"] = dict(
            zip(batches, [scrub["uns"] for scrub in scrubbed])
        )

        # Record that we've done batched analysis, so e.g. the plotting
        # function knows what to do.

        adata.uns["scrublet"]["batched_by"] = batch_key

    else:
        scrubbed = _run_scrublet(adata_obs, adata_sim)

        # Copy outcomes to input object from our processed version

        adata.obs["doublet_score"] = scrubbed["obs"]["doublet_score"]
        adata.obs["predicted_doublet"] = scrubbed["obs"]["predicted_doublet"]
        adata.uns["scrublet"] = scrubbed["uns"]

    logg.info("    Scrublet finished", time=start)

    return adata if copy else None


def _scrublet_call_doublets(
    adata_obs: AnnData,
    adata_sim: AnnData,
    *,
    n_neighbors: int | None = None,
    expected_doublet_rate: float = 0.05,
    stdev_doublet_rate: float = 0.02,
    mean_center: bool = True,
    normalize_variance: bool = True,
    n_prin_comps: int = 30,
    use_approx_neighbors: bool = True,
    knn_dist_metric: AnnoyDist = "euclidean",
    get_doublet_neighbor_parents: bool = False,
    threshold: float | None = None,
    random_state: AnyRandom = 0,
    verbose: bool = True,
) -> AnnData:
    """\
    Core function for predicting doublets using Scrublet [Wolock19]_.

    Predict cell doublets using a nearest-neighbor classifier of observed
    transcriptomes and simulated doublets. This is a wrapper around the core
    functions of `Scrublet <https://github.com/swolock/scrublet>`__ to allow
    for flexibility in applying Scanpy filtering operations upstream. Unless
    you know what you're doing you should use the main scrublet() function.

    .. note::
        More information and bug reports `here
        <https://github.com/swolock/scrublet>`__.

    Parameters
    ----------
    adata_obs
        The annotated data matrix of shape ``n_obs`` × ``n_vars``. Rows
        correspond to cells and columns to genes. Should be normalised with
        scanpy.pp.normalize_total() and filtered to include only highly
        variable genes.
    adata_sim
        Anndata object generated by
        :func:`~scanpy.pp.scrublet_simulate_doublets`, with same number of vars
        as adata_obs. This should have been built from adata_obs after
        filtering genes and cells and selcting highly-variable genes.
    n_neighbors
        Number of neighbors used to construct the KNN graph of observed
        transcriptomes and simulated doublets. If ``None``, this is
        automatically set to ``np.round(0.5 * np.sqrt(n_obs))``.
    expected_doublet_rate
        The estimated doublet rate for the experiment.
    stdev_doublet_rate
        Uncertainty in the expected doublet rate.
    mean_center
        If True, center the data such that each gene has a mean of 0.
        `sklearn.decomposition.PCA` will be used for dimensionality
        reduction.
    normalize_variance
        If True, normalize the data such that each gene has a variance of 1.
        `sklearn.decomposition.TruncatedSVD` will be used for dimensionality
        reduction, unless `mean_center` is True.
    n_prin_comps
        Number of principal components used to embed the transcriptomes prior
        to k-nearest-neighbor graph construction.
    use_approx_neighbors
        Use approximate nearest neighbor method (annoy) for the KNN
        classifier.
    knn_dist_metric
        Distance metric used when finding nearest neighbors. For list of
        valid values, see the documentation for annoy (if `use_approx_neighbors`
        is True) or sklearn.neighbors.NearestNeighbors (if `use_approx_neighbors`
        is False).
    get_doublet_neighbor_parents
        If True, return the parent transcriptomes that generated the
        doublet neighbors of each observed transcriptome. This information can
        be used to infer the cell states that generated a given
        doublet state.
    threshold
        Doublet score threshold for calling a transcriptome a doublet. If
        `None`, this is set automatically by looking for the minimum between
        the two modes of the `doublet_scores_sim_` histogram. It is best
        practice to check the threshold visually using the
        `doublet_scores_sim_` histogram and/or based on co-localization of
        predicted doublets in a 2-D embedding.
    random_state
        Initial state for doublet simulation and nearest neighbors.
    verbose
        If True, log progress updates.

    Returns
    -------
    adata : anndata.AnnData
        if ``copy=True`` it returns or else adds fields to ``adata``:

        ``.obs['doublet_score']``
            Doublet scores for each observed transcriptome

        ``.obs['predicted_doublets']``
            Boolean indicating predicted doublet status

        ``.uns['scrublet']['doublet_scores_sim']``
            Doublet scores for each simulated doublet transcriptome

        ``.uns['scrublet']['doublet_parents']``
            Pairs of ``.obs_names`` used to generate each simulated doublet transcriptome

        ``.uns['scrublet']['parameters']``
            Dictionary of Scrublet parameters
    """

    # Estimate n_neighbors if not provided, and create scrublet object.

    if n_neighbors is None:
        n_neighbors = int(round(0.5 * np.sqrt(adata_obs.shape[0])))

    # Note: Scrublet() will sparse adata_obs.X if it's not already, but this
    # matrix won't get used if we pre-set the normalised slots.

    scrub = Scrublet(
        adata_obs.X,
        n_neighbors=n_neighbors,
        expected_doublet_rate=expected_doublet_rate,
        stdev_doublet_rate=stdev_doublet_rate,
        random_state=random_state,
    )

    # Ensure normalised matrix sparseness as Scrublet does
    # https://github.com/swolock/scrublet/blob/67f8ecbad14e8e1aa9c89b43dac6638cebe38640/src/scrublet/scrublet.py#L100

    scrub._counts_obs_norm = sparse.csc_matrix(adata_obs.X)
    scrub._counts_sim_norm = sparse.csc_matrix(adata_sim.X)

    scrub.doublet_parents_ = adata_sim.obsm["doublet_parents"]

    # Call scrublet-specific preprocessing where specified

    if mean_center and normalize_variance:
        pipeline.zscore(scrub)
    elif mean_center:
        pipeline.mean_center(scrub)
    elif normalize_variance:
        pipeline.normalize_variance(scrub)

    # Do PCA. Scrublet fits to the observed matrix and decomposes both observed
    # and simulated based on that fit, so we'll just let it do its thing rather
    # than trying to use Scanpy's PCA wrapper of the same functions.

    if mean_center:
        logg.info("Embedding transcriptomes using PCA...")
        pipeline.pca(scrub, n_prin_comps=n_prin_comps, random_state=scrub._random_state)
    else:
        logg.info("Embedding transcriptomes using Truncated SVD...")
        pipeline.truncated_svd(
            scrub, n_prin_comps=n_prin_comps, random_state=scrub._random_state
        )

    # Score the doublets

    scrub.calculate_doublet_scores(
        use_approx_neighbors=use_approx_neighbors,
        distance_metric=knn_dist_metric,
        get_doublet_neighbor_parents=get_doublet_neighbor_parents,
    )

    # Actually call doublets

    scrub.call_doublets(threshold=threshold, verbose=verbose)

    # Store results in AnnData for return

    adata_obs.obs["doublet_score"] = scrub.doublet_scores_obs_

    # Store doublet Scrublet metadata

    adata_obs.uns["scrublet"] = {
        "doublet_scores_sim": scrub.doublet_scores_sim_,
        "doublet_parents": adata_sim.obsm["doublet_parents"],
        "parameters": {
            "expected_doublet_rate": expected_doublet_rate,
            "sim_doublet_ratio": (
                adata_sim.uns.get("scrublet", {})
                .get("parameters", {})
                .get("sim_doublet_ratio", None)
            ),
            "n_neighbors": n_neighbors,
            "random_state": random_state,
        },
    }

    # If threshold hasn't been located successfully then we couldn't make any
    # predictions. The user will get a warning from Scrublet, but we need to
    # set the boolean so that any downstream filtering on
    # predicted_doublet=False doesn't incorrectly filter cells. The user can
    # still use this object to generate the plot and derive a threshold
    # manually.

    if hasattr(scrub, "threshold_"):
        adata_obs.uns["scrublet"]["threshold"] = scrub.threshold_
        adata_obs.obs["predicted_doublet"] = scrub.predicted_doublets_
    else:
        adata_obs.obs["predicted_doublet"] = False

    if get_doublet_neighbor_parents:
        adata_obs.uns["scrublet"][
            "doublet_neighbor_parents"
        ] = scrub.doublet_neighbor_parents_

    return adata_obs


def scrublet_simulate_doublets(
    adata: AnnData,
    *,
    layer: str | None = None,
    sim_doublet_ratio: float = 2.0,
    synthetic_doublet_umi_subsampling: float = 1.0,
    random_seed: AnyRandom = 0,
) -> AnnData:
    """\
    Simulate doublets by adding the counts of random observed transcriptome pairs.

    Parameters
    ----------
    adata
        The annotated data matrix of shape ``n_obs`` × ``n_vars``. Rows
        correspond to cells and columns to genes. Genes should have been
        filtered for expression and variability, and the object should contain
        raw expression of the same dimensions.
    layer
        Layer of adata where raw values are stored, or 'X' if values are in .X.
    sim_doublet_ratio
        Number of doublets to simulate relative to the number of observed
        transcriptomes. If `None`, self.sim_doublet_ratio is used.
    synthetic_doublet_umi_subsampling
        Rate for sampling UMIs when creating synthetic doublets. If 1.0,
        each doublet is created by simply adding the UMIs from two randomly
        sampled observed transcriptomes. For values less than 1, the
        UMI counts are added and then randomly sampled at the specified
        rate.

    Returns
    -------
    adata : anndata.AnnData with simulated doublets in .X
        Adds fields to ``adata``:

        ``.obsm['scrublet']['doublet_parents']``
            Pairs of ``.obs_names`` used to generate each simulated doublet transcriptome

        ``.uns['scrublet']['parameters']``
            Dictionary of Scrublet parameters

    See also
    --------
    :func:`~scanpy.pp.scrublet`: Main way of running Scrublet, runs
        preprocessing, doublet simulation (this function) and calling.
    :func:`~scanpy.pl.scrublet_score_distribution`: Plot histogram of doublet
        scores for observed transcriptomes and simulated doublets.
    """

    X = _get_obs_rep(adata, layer=layer)
    scrub = Scrublet(X, random_state=random_seed)

    scrub.simulate_doublets(
        sim_doublet_ratio=sim_doublet_ratio,
        synthetic_doublet_umi_subsampling=synthetic_doublet_umi_subsampling,
    )

    adata_sim = AnnData(scrub._counts_sim)
    adata_sim.obs["n_counts"] = scrub._total_counts_sim
    adata_sim.obsm["doublet_parents"] = scrub.doublet_parents_
    adata_sim.uns["scrublet"] = {"parameters": {"sim_doublet_ratio": sim_doublet_ratio}}
    return adata_sim
