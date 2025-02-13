#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Contains the dynamic nested sampler class :class:`DynamicSampler` used to
dynamically allocate nested samples. Note that :class:`DynamicSampler`
implicitly wraps a sampler from :mod:`~dynesty.nestedsamplers`. Also contains
the weight function :meth:`weight_function` and stopping function
:meth:`stopping_function`. These are used by default within
:class:`DynamicSampler` if corresponding functions are not provided
by the user.

"""

import sys
import warnings
from functools import partial
import math
import numpy as np
import copy
from .utils import get_seed_sequence
from scipy.special import logsumexp

try:
    import tqdm
except ImportError:
    tqdm = None

from .nestedsamplers import (UnitCubeSampler, SingleEllipsoidSampler,
                             MultiEllipsoidSampler, RadFriendsSampler,
                             SupFriendsSampler)
from .results import Results, print_fn
from .utils import kld_error, get_random_generator

__all__ = [
    "DynamicSampler", "weight_function", "stopping_function", "_kld_error"
]

_SAMPLERS = {
    'none': UnitCubeSampler,
    'single': SingleEllipsoidSampler,
    'multi': MultiEllipsoidSampler,
    'balls': RadFriendsSampler,
    'cubes': SupFriendsSampler
}

SQRTEPS = math.sqrt(float(np.finfo(np.float64).eps))


def _kld_error(args):
    """ Internal `pool.map`-friendly wrapper for :meth:`kld_error` used by
    :meth:`stopping_function`."""

    # Extract arguments.
    results, error, approx, rseed = args
    rstate = get_random_generator(rseed)
    return kld_error(results,
                     error,
                     rstate=rstate,
                     return_new=True,
                     approx=approx)


def weight_function(results, args=None, return_weights=False):
    """
    The default weight function utilized by :class:`DynamicSampler`.
    Zipped parameters are passed to the function via :data:`args`.
    Assigns each point a weight based on a weighted average of the
    posterior and evidence information content::

        weight = pfrac * pweight + (1. - pfrac) * zweight

    where `pfrac` is the fractional importance placed on the posterior,
    the evidence weight `zweight` is based on the estimated remaining
    posterior mass, and the posterior weight `pweight` is the sample's
    importance weight.

    Returns a set of log-likelihood bounds set by the earliest/latest
    samples where `weight > maxfrac * max(weight)`, with additional
    left/right padding based on `pad`.

    Parameters
    ----------
    results : :class:`Results` instance
        :class:`Results` instance.

    args : dictionary of keyword arguments, optional
        Arguments used to set the log-likelihood bounds used for sampling,
        as described above. Default values are `pfrac = 0.8`, `maxfrac = 0.8`,
        and `pad = 1`.

    return_weights : bool, optional
        Whether to return the individual weights (and their components) used
        to compute the log-likelihood bounds. Default is `False`.

    Returns
    -------
    logl_bounds : tuple with shape (2,)
        Log-likelihood bounds `(logl_min, logl_max)` determined by the weights.

    weights : tuple with shape (3,), optional
        The individual weights `(pweight, zweight, weight)` used to determine
        `logl_bounds`.

    """

    # Initialize hyperparameters.
    if args is None:
        args = dict({})
    pfrac = args.get('pfrac', 0.8)
    if not 0. <= pfrac <= 1.:
        raise ValueError(
            "The provided `pfrac` {0} is not between 0. and 1.".format(pfrac))
    maxfrac = args.get('maxfrac', 0.8)
    if not 0. <= maxfrac <= 1.:
        raise ValueError(
            "The provided `maxfrac` {0} is not between 0. and 1.".format(
                maxfrac))
    lpad = args.get('pad', 1)
    if lpad < 0:
        raise ValueError("`lpad` {0} is less than zero.".format(lpad))

    # Derive evidence weights.
    logz = results.logz  # final ln(evidence)
    logz_remain = results.logl[-1] + results.logvol[-1]  # remainder
    logz_tot = np.logaddexp(logz[-1], logz_remain)  # estimated upper bound
    lzones = np.ones_like(logz)
    logzin = logsumexp([lzones * logz_tot, logz], axis=0,
                       b=[lzones, -lzones])  # ln(remaining evidence)
    logzweight = logzin - np.log(results.samples_n)  # ln(evidence weight)
    logzweight -= logsumexp(logzweight)  # normalize
    zweight = np.exp(logzweight)  # convert to linear scale

    # Derive posterior weights.
    pweight = np.exp(results.logwt - results.logz[-1])  # importance weight
    pweight /= sum(pweight)  # normalize

    # Compute combined weights.
    weight = (1. - pfrac) * zweight + pfrac * pweight

    # Compute logl bounds
    nsamps = len(logz)
    bounds = np.arange(nsamps)[weight > maxfrac * max(weight)]
    bounds = (min(bounds) - lpad, min(max(bounds) + lpad, nsamps - 1))
    if bounds[0] < 0:
        logl_min = -np.inf
    else:
        logl_min = results.logl[bounds[0]]
    logl_max = results.logl[bounds[1]]

    if return_weights:
        return (logl_min, logl_max), (pweight, zweight, weight)
    else:
        return (logl_min, logl_max)


def __get_update_interval_ratio(update_interval, sample, bound, ndim, nlive,
                                slices, walks):
    """ 
    Get the update_interval divided by the number of live points
    """
    if update_interval is None:
        if sample == 'unif':
            update_interval_frac = 1.5
        elif sample == 'rwalk' or sample == 'rstagger':
            update_interval_frac = 0.15 * walks
        elif sample == 'slice':
            update_interval_frac = 0.9 * ndim * slices
        elif sample == 'rslice':
            update_interval_frac = 2.0 * slices
        elif sample == 'hslice':
            update_interval_frac = 25.0 * slices
        else:
            raise ValueError("Unknown sampling method: '{0}'".format(sample))
    elif isinstance(update_interval, float):
        update_interval_frac = update_interval
    elif isinstance(update_interval, int):
        update_interval_frac = update_interval * 1. / nlive
    else:
        raise RuntimeError(
            str.format('Strange update_interval value {}', update_interval))
    if bound == 'none':
        update_interval_frac = np.inf
    return update_interval_frac


def stopping_function(results,
                      args=None,
                      rstate=None,
                      M=None,
                      return_vals=False):
    """
    The default stopping function utilized by :class:`DynamicSampler`.
    Zipped parameters are passed to the function via :data:`args`.
    Assigns the run a stopping value based on a weighted average of the
    stopping values for the posterior and evidence::

        stop = pfrac * stop_post + (1.- pfrac) * stop_evid

    The evidence stopping value is based on the estimated evidence error
    (i.e. standard deviation) relative to a given threshold::

        stop_evid = evid_std / evid_thresh

    The posterior stopping value is based on the fractional error (i.e.
    standard deviation / mean) in the Kullback-Leibler (KL) divergence
    relative to a given threshold::

        stop_post = (kld_std / kld_mean) / post_thresh

    Estimates of the mean and standard deviation are computed using `n_mc`
    realizations of the input using a provided `'error'` keyword (either
    `'jitter'` or `'simulate'`, which call related functions :meth:`jitter_run`
    and :meth:`simulate_run` in :mod:`dynesty.utils`, respectively, or
    `'sim_approx'`, which boosts `'jitter'` by a factor of two).

    Returns the boolean `stop <= 1`. If `True`, the :class:`DynamicSampler`
    will stop adding new samples to our results.

    Parameters
    ----------
    results : :class:`Results` instance
        :class:`Results` instance.

    args : dictionary of keyword arguments, optional
        Arguments used to set the stopping values. Default values are
        `pfrac = 1.0`, `evid_thresh = 0.1`, `post_thresh = 0.02`,
        `n_mc = 128`, `error = 'sim_approx'`, and `approx = True`.

    rstate : `~numpy.random.Generator`, optional
        `~numpy.random.Generator` instance.

    M : `map` function, optional
        An alias to a `map`-like function. This allows users to pass
        functions from pools (e.g., `pool.map`) to compute realizations in
        parallel. By default the standard `map` function is used.

    return_vals : bool, optional
        Whether to return the stopping value (and its components). Default
        is `False`.

    Returns
    -------
    stop_flag : bool
        Boolean flag indicating whether we have passed the desired stopping
        criteria.

    stop_vals : tuple of shape (3,), optional
        The individual stopping values `(stop_post, stop_evid, stop)` used
        to determine the stopping criteria.

    """

    # Initialize values.
    if args is None:
        args = dict({})
    if M is None:
        M = map

    # Initialize hyperparameters.
    pfrac = args.get('pfrac', 1.0)
    if not 0. <= pfrac <= 1.:
        raise ValueError(
            "The provided `pfrac` {0} is not between 0. and 1.".format(pfrac))
    evid_thresh = args.get('evid_thresh', 0.1)
    if pfrac < 1. and evid_thresh < 0.:
        raise ValueError("The provided `evid_thresh` {0} is not non-negative "
                         "even though `1. - pfrac` is {1}.".format(
                             evid_thresh, 1. - pfrac))
    post_thresh = args.get('post_thresh', 0.02)
    if pfrac > 0. and post_thresh < 0.:
        raise ValueError("The provided `post_thresh` {0} is not non-negative "
                         "even though `pfrac` is {1}.".format(
                             post_thresh, pfrac))
    n_mc = args.get('n_mc', 128)
    if n_mc <= 1:
        raise ValueError("The number of realizations {0} must be greater "
                         "than 1.".format(n_mc))
    elif n_mc < 20:
        warnings.warn("Using a small number of realizations might result in "
                      "excessively noisy stopping value estimates.")
    error = args.get('error', 'sim_approx')
    if error not in {'jitter', 'simulate', 'sim_approx'}:
        raise ValueError(
            "The chosen `'error'` option {0} is not valid.".format(error))
    if error == 'sim_approx':
        error = 'jitter'
        boost = 2.
    else:
        boost = 1.
    approx = args.get('approx', True)

    # Compute realizations of ln(evidence) and the KL divergence.
    rlist = [results for i in range(n_mc)]
    error_list = [error for i in range(n_mc)]
    approx_list = [approx for i in range(n_mc)]
    seeds = get_seed_sequence(rstate, n_mc)
    args = zip(rlist, error_list, approx_list, seeds)
    outputs = list(M(_kld_error, args))
    kld_arr, lnz_arr = np.array([(kld[-1], res.logz[-1])
                                 for kld, res in outputs]).T

    # Evidence stopping value.
    lnz_std = np.std(lnz_arr)
    stop_evid = np.sqrt(boost) * lnz_std / evid_thresh

    # Posterior stopping value.
    kld_mean, kld_std = np.mean(kld_arr), np.std(kld_arr)
    stop_post = boost * (kld_std / kld_mean) / post_thresh

    # Effective stopping value.
    stop = pfrac * stop_post + (1. - pfrac) * stop_evid

    if return_vals:
        return stop <= 1., (stop_post, stop_evid, stop)
    else:
        return stop <= 1.


class RunRecord:
    def __init__(self):
        # results
        D = {}
        D['id'] = []  # live point labels
        D['u'] = []  # unit cube samples
        D['v'] = []  # transformed variable samples
        D['logl'] = []  # loglikelihoods of samples
        D['logvol'] = []  # expected ln(volume)
        D['logwt'] = []  # ln(weights)
        D['logz'] = []  # cumulative ln(evidence)
        D['logzvar'] = []  # cumulative error on ln(evidence)
        D['h'] = []  # cumulative information
        D['nc'] = []  # number of calls at each iteration
        D['boundidx'] = []  # index of bound dead point was drawn from
        D['it'] = []  # iteration the live (now dead) point was proposed
        D['n'] = []  # number of live points interior to dead point
        D['bounditer'] = []  # active bound at a specific iteration
        D['scale'] = []  # scale factor at each iteration
        D['batch'] = []  # live point batch ID
        # these are special since their length is == the number of batches
        D['batch_nlive'] = []  # number of live points added in batch
        D['batch_bounds'] = []  # loglikelihood bounds used in batch
        self.D = D

    def append(self, newD):
        for k in newD.keys():
            self.D[k].append(newD[k])


class DynamicSampler(object):
    """
    A dynamic nested sampler that allocates live points adaptively during
    a single run according to a specified weight function until a specified
    stopping criteria is reached.

    Parameters
    ----------
    loglikelihood : function
        Function returning ln(likelihood) given parameters as a 1-d `~numpy`
        array of length `ndim`.

    prior_transform : function
        Function transforming a sample from the a unit cube to the parameter
        space of interest according to the prior.

    npdim : int, optional
        Number of parameters accepted by `prior_transform`.

    bound : {`'none'`, `'single'`, `'multi'`, `'balls'`, `'cubes'`}, optional
        Method used to approximately bound the prior using the current
        set of live points. Conditions the sampling methods used to
        propose new live points.

    method : {`'unif'`, `'rwalk'`, `'rstagger'`,
              `'slice'`, `'rslice'`, `'hslice'`}, optional
        Method used to sample uniformly within the likelihood constraint,
        conditioned on the provided bounds.

    update_interval : int
        Only update the bounding distribution every `update_interval`-th
        likelihood call.

    first_update : dict
        A dictionary containing parameters governing when the sampler should
        first update the bounding distribution from the unit cube to the one
        specified by the user.

    rstate : `~numpy.random.Generator`
        `~numpy.random.Generator` instance.

    queue_size: int
        Carry out likelihood evaluations in parallel by queueing up new live
        point proposals using (at most) this many threads/members.

    pool: pool
        Use this pool of workers to execute operations in parallel.

    use_pool : dict, optional
        A dictionary containing flags indicating where the provided `pool`
        should be used to execute operations in parallel.

    kwargs : dict, optional
        A dictionary of additional parameters (described below).

    """
    def __init__(self, loglikelihood, prior_transform, npdim, bound, method,
                 update_interval_ratio, first_update, rstate, queue_size, pool,
                 use_pool, ncdim, nlive0, kwargs):

        # distributions
        self.loglikelihood = loglikelihood
        self.prior_transform = prior_transform
        self.npdim = npdim
        self.ncdim = ncdim

        # bounding/sampling
        self.bounding = bound
        self.method = method
        self.update_interval_ratio = update_interval_ratio
        self.first_update = first_update

        # internal sampler object
        self.sampler = None

        # extra arguments
        self.kwargs = kwargs
        if kwargs.get('bootstrap') is None:
            if self.method == 'unif':
                self.bootstrap = 20
            else:
                self.bootstrap = 0
        else:
            self.bootstrap = kwargs.get('bootstrap')
        if self.bootstrap > 0:
            self.enlarge = kwargs.get('enlarge', 1.0)
        else:
            self.enlarge = kwargs.get('enlarge', 1.25)
        self.walks = self.kwargs.get('walks', 25)
        self.slices = self.kwargs.get('slices', 3)
        self.cite = self.kwargs.get('cite')
        self.custom_update = self.kwargs.get('update_func')

        # random state
        self.rstate = rstate

        # parallelism
        self.queue_size = queue_size
        self.pool = pool
        if self.pool is None:
            self.M = map
        else:
            self.M = pool.map

        self.use_pool = use_pool  # provided flags for when to use the pool
        self.use_pool_ptform = use_pool.get('prior_transform', True)
        self.use_pool_logl = use_pool.get('loglikelihood', True)
        self.use_pool_evolve = use_pool.get('propose_point', True)
        self.use_pool_update = use_pool.get('update_bound', True)
        self.use_pool_stopfn = use_pool.get('stop_function', True)

        # sampling details
        self.it = 1  # number of iterations
        self.batch = 0  # number of batches allocated dynamically
        self.ncall = 0  # number of function calls
        self.bound = []  # initial states used to compute bounds
        self.eff = 1.  # sampling efficiency
        self.base = False  # base run complete
        self.nlive0 = nlive0

        self.saved_run = RunRecord()
        self.base_run = RunRecord()
        self.new_run = RunRecord()

        self.new_logl_min, self.new_logl_max = -np.inf, np.inf  # logl bounds

    def __getstate__(self):
        """Get state information for pickling."""

        state = self.__dict__.copy()

        del state['rstate']  # remove random module

        # deal with pool
        if state['pool'] is not None:
            del state['pool']  # remove pool
            del state['M']  # remove `pool.map` function hook

        # deal with internal sampler (to be safe)
        if state['sampler'] is not None:
            try:  # attempt to remove the same things
                del state['sampler'].rstate
                if state['sampler'].pool is not None:
                    del state['sampler'].pool
                    del state['sampler'].M
            except AttributeError:
                pass

        return state

    def __get_update_interval(self, update_interval, nlive):
        if not isinstance(update_interval, int):
            if isinstance(update_interval, float):
                cur_update_interval_ratio = update_interval
            elif update_interval is None:
                cur_update_interval_ratio = self.update_interval_ratio
            else:
                raise RuntimeError(
                    str.format('Weird update_interval value {}',
                               update_interval))
            update_interval = int(
                max(
                    min(np.round(cur_update_interval_ratio * nlive),
                        sys.maxsize), 1))
        return update_interval

    def reset(self):
        """Re-initialize the sampler."""

        # sampling
        self.it = 1
        self.batch = 0
        self.ncall = 0
        self.bound = []
        self.eff = 1.
        self.base = False

        self.saved_run = RunRecord()
        self.base_run = RunRecord()
        self.new_run = RunRecord()
        self.new_logl_min, self.new_logl_max = -np.inf, np.inf

    @property
    def results(self):
        """Saved results from the dynamic nested sampling run. All saved
        bounds are also returned."""
        d = {}
        for k in [
                'nc', 'v', 'id', 'batch', 'it', 'u', 'n', 'logwt', 'logl',
                'logvol', 'logz', 'logzvar', 'h', 'batch_nlive', 'batch_bounds'
        ]:
            d[k] = np.array(self.saved_run.D[k])

        # Add all saved samples (and ancillary quantities) to the results.
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = [('niter', self.it - 1), ('ncall', d['nc']),
                       ('eff', self.eff), ('samples', d['v'])]
            for k in ['id', 'batch', 'it', 'u', 'n']:
                results.append(('samples_' + k, d[k]))
            for k in [
                    'logwt', 'logl', 'logvol', 'logz', 'batch_nlive',
                    'batch_bounds'
            ]:
                results.append((k, d[k]))
            results.append(('logzerr', np.sqrt(d['logzvar'])))
            results.append(('information', d['h']))

        # Add any saved bounds (and ancillary quantities) to the results.
        if self.sampler.save_bounds:
            results.append(('bound', copy.deepcopy(self.bound)))
            results.append(
                ('bound_iter', np.array(self.saved_run.D['bounditer'])))
            results.append(
                ('samples_bound', np.array(self.saved_run.D['boundidx'])))
            results.append(('scale', np.array(self.saved_run.D['scale'])))

        return Results(results)

    @property
    def n_effective(self):
        """
        Estimate the effective number of posterior samples using the Kish
        Effective Sample Size (ESS) where `ESS = sum(wts)^2 / sum(wts^2)`.
        Note that this is `len(wts)` when `wts` are uniform and
        `1` if there is only one non-zero element in `wts`.

        """

        if len(self.saved_run.D['logwt']) == 0:
            # If there are no saved weights, return 0.
            return 0
        else:
            # Otherwise, compute Kish ESS.
            logwts = np.array(self.saved_run.D['logwt'])
            logneff = logsumexp(logwts) * 2 - logsumexp(logwts * 2)
            return np.exp(logneff)

    @property
    def citations(self):
        """
        Return list of papers that should be cited given the specified
        configuration of the sampler.

        """

        return self.cite

    def sample_initial(self,
                       nlive=None,
                       update_interval=None,
                       first_update=None,
                       maxiter=None,
                       maxcall=None,
                       logl_max=np.inf,
                       dlogz=0.01,
                       n_effective=np.inf,
                       live_points=None,
                       save_samples=False,
                       resume=False):
        """
        Generate a series of initial samples from a nested sampling
        run using a fixed number of live points using an internal
        sampler from :mod:`~dynesty.nestedsamplers`. Instantiates a
        generator that will be called by the user.

        Parameters
        ----------
        nlive : int, optional
            The number of live points to use for the baseline nested
            sampling run. Default is either nlive0 parameter of 500 

        update_interval : int or float, optional
            If an integer is passed, only update the bounding distribution
            every `update_interval`-th likelihood call. If a float is passed,
            update the bound after every `round(update_interval * nlive)`-th
            likelihood call. Larger update intervals can be more efficient
            when the likelihood function is quick to evaluate. If no value is
            provided, defaults to the value passed during initialization.

        first_update : dict, optional
            A dictionary containing parameters governing when the sampler will
            first update the bounding distribution from the unit cube
            (`'none'`) to the one specified by `sample`.

        maxiter : int, optional
            Maximum number of iterations. Iteration may stop earlier if the
            termination condition is reached. Default is `sys.maxsize`
            (no limit).

        maxcall : int, optional
            Maximum number of likelihood evaluations. Iteration may stop
            earlier if termination condition is reached. Default is
            `sys.maxsize` (no limit).

        dlogz : float, optional
            Iteration will stop when the estimated contribution of the
            remaining prior volume to the total evidence falls below
            this threshold. Explicitly, the stopping criterion is
            `ln(z + z_est) - ln(z) < dlogz`, where `z` is the current
            evidence from all saved samples and `z_est` is the estimated
            contribution from the remaining volume. The default is
            `0.01`.

        logl_max : float, optional
            Iteration will stop when the sampled ln(likelihood) exceeds the
            threshold set by `logl_max`. Default is no bound (`np.inf`).

        n_effective: int, optional
            Target number of effective posterior samples. If the estimated
            "effective sample size" (ESS) exceeds this number,
            sampling will terminate. Default is no ESS (`np.inf`).

        live_points : list of 3 `~numpy.ndarray` each with shape (nlive, ndim)
            A set of live points used to initialize the nested sampling run.
            Contains `live_u`, the coordinates on the unit cube, `live_v`, the
            transformed variables, and `live_logl`, the associated
            loglikelihoods. By default, if these are not provided the initial
            set of live points will be drawn from the unit `npdim`-cube.
            **WARNING: It is crucial that the initial set of live points have
            been sampled from the prior. Failure to provide a set of valid
            live points will lead to incorrect results.**

        Returns
        -------
        worst : int
            Index of the live point with the worst likelihood. This is our
            new dead point sample.

        ustar : `~numpy.ndarray` with shape (npdim,)
            Position of the sample.

        vstar : `~numpy.ndarray` with shape (ndim,)
            Transformed position of the sample.

        loglstar : float
            Ln(likelihood) of the sample.

        logvol : float
            Ln(prior volume) within the sample.

        logwt : float
            Ln(weight) of the sample.

        logz : float
            Cumulative ln(evidence) up to the sample (inclusive).

        logzvar : float
            Estimated cumulative variance on `logz` (inclusive).

        h : float
            Cumulative information up to the sample (inclusive).

        nc : int
            Number of likelihood calls performed before the new
            live point was accepted.

        worst_it : int
            Iteration when the live (now dead) point was originally proposed.

        boundidx : int
            Index of the bound the dead point was originally drawn from.

        bounditer : int
            Index of the bound being used at the current iteration.

        eff : float
            The cumulative sampling efficiency (in percent).

        delta_logz : float
            The estimated remaining evidence expressed as the ln(ratio) of the
            current evidence.

        """

        # Initialize inputs.
        if maxcall is None:
            maxcall = sys.maxsize
        if maxiter is None:
            maxiter = sys.maxsize
        if nlive <= 2 * self.ncdim:
            warnings.warn("Beware: `nlive_init <= 2 * ndim`!")

        update_interval = self.__get_update_interval(update_interval, nlive)
        nlive = nlive or self.nlive0

        if not resume:
            # Reset saved results to avoid any possible conflicts.
            self.reset()

            # Initialize the first set of live points.
            if live_points is None:
                # If no live points are provided, propose them by randomly
                # sampling from the unit cube.
                self.nlive_init = nlive
                for attempt in range(100):
                    self.live_u = self.rstate.uniform(size=(self.nlive_init,
                                                            self.npdim))
                    if self.use_pool_ptform:
                        self.live_v = np.array(
                            list(
                                self.M(self.prior_transform,
                                       np.array(self.live_u))))
                    else:
                        self.live_v = np.array(
                            list(
                                map(self.prior_transform,
                                    np.array(self.live_u))))
                    self.live_logl = np.array(
                        self.loglikelihood.map(np.array(self.live_v)))

                    # Convert all `-np.inf` log-likelihoods to finite large
                    # numbers. Necessary to keep estimators in our sampler from
                    # breaking.
                    for i, logl in enumerate(self.live_logl):
                        if not np.isfinite(logl):
                            if np.sign(logl) < 0:
                                self.live_logl[i] = -1e300
                            else:
                                raise ValueError(
                                    "The log-likelihood ({0}) of live "
                                    "point {1} located at u={2} v={3} "
                                    " is invalid.".format(
                                        logl, i, self.live_u[i],
                                        self.live_v[i]))

                    # Check to make sure there is at least one finite
                    # log-likelihood value within the initial set of live
                    # points.
                    if any(self.live_logl != -1e300):
                        break
                else:
                    # If we found nothing after many attempts, raise the alarm.
                    raise RuntimeError(
                        "After many attempts, not a single live "
                        "point had a valid log-likelihood! Please "
                        "check your prior transform and/or "
                        "log-likelihood.")
            else:
                # If live points were provided, convert the log-likelihoods and
                # then run a quick safety check.
                self.live_u, self.live_v, self.live_logl = live_points
                self.nlive_init = len(self.live_u)

                for i, logl in enumerate(self.live_logl):
                    if not np.isfinite(logl):
                        if np.sign(logl) < 0:
                            self.live_logl[i] = -1e300
                        else:
                            raise ValueError(
                                "The log-likelihood ({0}) of live "
                                "point {1} located at u={2} v={3} "
                                " is invalid.".format(logl, i, self.live_u[i],
                                                      self.live_v[i]))
                if all(self.live_logl == -1e300):
                    raise ValueError("Not a single provided live point has a "
                                     "valid log-likelihood!")

            # (Re-)bundle live points.
            live_points = [self.live_u, self.live_v, self.live_logl]
            self.live_init = [np.array(_) for _ in live_points]
            self.ncall += self.nlive_init
            self.live_bound = np.zeros(self.nlive_init, dtype='int')
            self.live_it = np.zeros(self.nlive_init, dtype='int')

            bounding = self.bounding

            if first_update is None:
                first_update = self.first_update
            self.sampler = _SAMPLERS[bounding](self.loglikelihood,
                                               self.prior_transform,
                                               self.npdim,
                                               self.live_init,
                                               self.method,
                                               update_interval,
                                               first_update,
                                               self.rstate,
                                               self.queue_size,
                                               self.pool,
                                               self.use_pool,
                                               ncdim=self.ncdim,
                                               kwargs=self.kwargs)
            self.bound = self.sampler.bound

        # Run the sampler internally as a generator.
        for i in range(1):
            for it, results in enumerate(
                    self.sampler.sample(maxiter=maxiter,
                                        save_samples=save_samples,
                                        maxcall=maxcall,
                                        dlogz=dlogz,
                                        n_effective=n_effective)):
                # Grab results.
                (worst, ustar, vstar, loglstar, logvol, logwt, logz, logzvar,
                 h, nc, worst_it, boundidx, bounditer, eff,
                 delta_logz) = results

                # Save our base run (which we will use later).
                add_info = dict(id=worst,
                                u=ustar,
                                v=vstar,
                                logl=loglstar,
                                logvol=logvol,
                                logwt=logwt,
                                logz=logz,
                                logzvar=logzvar,
                                h=h,
                                nc=nc,
                                it=worst_it,
                                n=self.nlive_init,
                                boundidx=boundidx,
                                bounditer=bounditer,
                                scale=self.sampler.scale)

                self.base_run.append(add_info)
                self.saved_run.append(add_info)

                # Increment relevant counters.
                self.ncall += nc
                self.eff = 100. * self.it / self.ncall
                self.it += 1

                yield (worst, ustar, vstar, loglstar, logvol, logwt, logz,
                       logzvar, h, nc, worst_it, boundidx, bounditer, self.eff,
                       delta_logz)

            for it, results in enumerate(self.sampler.add_live_points()):
                # Grab results.
                (worst, ustar, vstar, loglstar, logvol, logwt, logz, logzvar,
                 h, nc, worst_it, boundidx, bounditer, eff,
                 delta_logz) = results

                add_info = dict(id=worst,
                                u=ustar,
                                v=vstar,
                                logl=loglstar,
                                logvol=logvol,
                                logwt=logwt,
                                logz=logz,
                                logzvar=logzvar,
                                h=h,
                                nc=nc,
                                it=worst_it,
                                n=self.nlive_init - it,
                                boundidx=boundidx,
                                bounditer=bounditer,
                                scale=self.sampler.scale)

                self.base_run.append(add_info)
                self.saved_run.append(add_info)

                # Increment relevant counters.
                self.eff = 100. * self.it / self.ncall
                self.it += 1

                yield (worst, ustar, vstar, loglstar, logvol, logwt, logz,
                       logzvar, h, nc, worst_it, boundidx, bounditer, self.eff,
                       delta_logz)

        self.base = True  # baseline run complete
        self.saved_run.D['batch'] = np.zeros(len(self.saved_run.D['id']),
                                             dtype='int')  # batch

        self.saved_run.D['batch_nlive'].append(
            self.nlive_init)  # initial nlive
        self.saved_run.D['batch_bounds'].append(
            (-np.inf, np.inf))  # initial bounds

    def sample_batch(self,
                     nlive_new=None,
                     update_interval=None,
                     logl_bounds=None,
                     maxiter=None,
                     maxcall=None,
                     save_bounds=True):
        """
        Generate an additional series of nested samples that will be combined
        with the previous set of dead points. Works by hacking the internal
        `sampler` object.
        Instantiates a generator that will be called by the user.

        Parameters
        ----------
        nlive_new : int
            Number of new live points to be added. Default is `500`.

        update_interval : int or float, optional
            If an integer is passed, only update the bounding distribution
            every `update_interval`-th likelihood call. If a float is passed,
            update the bound after every `round(update_interval * nlive)`-th
            likelihood call. Larger update intervals can be more efficient
            when the likelihood function is quick to evaluate. If no value is
            provided, defaults to the value passed during initialization.

        logl_bounds : tuple of size (2,), optional
            The ln(likelihood) bounds used to bracket the run. If `None`,
            the default bounds span the entire range covered by the
            original run.

        maxiter : int, optional
            Maximum number of iterations. Iteration may stop earlier if the
            termination condition is reached. Default is `sys.maxsize`
            (no limit).

        maxcall : int, optional
            Maximum number of likelihood evaluations. Iteration may stop
            earlier if termination condition is reached. Default is
            `sys.maxsize` (no limit).

        save_bounds : bool, optional
            Whether or not to save past distributions used to bound
            the live points internally. Default is `True`.

        Returns
        -------
        worst : int
            Index of the live point with the worst likelihood. This is our
            new dead point sample. **Negative values indicate the index
            of a new live point generated when initializing a new batch.**

        ustar : `~numpy.ndarray` with shape (npdim,)
            Position of the sample.

        vstar : `~numpy.ndarray` with shape (ndim,)
            Transformed position of the sample.

        loglstar : float
            Ln(likelihood) of the sample.

        nc : int
            Number of likelihood calls performed before the new
            live point was accepted.

        worst_it : int
            Iteration when the live (now dead) point was originally proposed.

        boundidx : int
            Index of the bound the dead point was originally drawn from.

        bounditer : int
            Index of the bound being used at the current iteration.

        eff : float
            The cumulative sampling efficiency (in percent).

        """

        # Initialize default values.
        if maxcall is None:
            maxcall = sys.maxsize
        if maxiter is None:
            maxiter = sys.maxsize
        nlive_new = nlive_new or self.nlive0

        if nlive_new <= 2 * self.ncdim:
            warnings.warn("Beware: `nlive_batch <= 2 * ndim`!")
        self.sampler.save_bounds = save_bounds

        dlogz_batch = 1e-4
        # This is the target dlogz in batch
        # Previously this was zero, but that could make the sampler stuck
        # if the narrow posterior mode is missed

        # Initialize starting values.
        h = 0.0  # Information, initially *0.*
        logz = -1.e300  # ln(evidence), initially *0.*
        logvol = 0.  # initially contains the whole prior (volume=1.)

        # Grab results from saved run.
        saved_u = np.array(self.saved_run.D['u'])
        saved_v = np.array(self.saved_run.D['v'])
        saved_logl = np.array(self.saved_run.D['logl'])
        saved_logwt = np.array(self.saved_run.D['logwt'])
        saved_scale = np.array(self.saved_run.D['scale'])
        nblive = self.nlive_init

        update_interval = self.__get_update_interval(update_interval,
                                                     nlive_new)
        self.sampler.update_interval = update_interval

        # Reset "new" results.
        self.new_run = RunRecord()
        self.new_logl_min, self.new_logl_max = -np.inf, np.inf

        # Initialize ln(likelihood) bounds.
        if logl_bounds is None:
            logl_min, logl_max = -np.inf, max(saved_logl[:-nblive])
            # why is logl_max defined this way ???
            # why are we skipping top nblive points ?
        else:
            logl_min, logl_max = logl_bounds
        self.new_logl_min, self.new_logl_max = logl_min, logl_max

        # Check whether the lower bound encompasses all previous saved samples.
        psel = np.all(logl_min <= saved_logl)
        if psel:
            # If the lower bound encompasses all saved samples, we want
            # to propose a new set of points from the unit cube.
            live_u = self.rstate.uniform(size=(nlive_new, self.npdim))
            if self.use_pool_ptform:
                live_v = np.array(
                    list(self.M(self.prior_transform, np.array(live_u))))
            else:
                live_v = np.array(
                    list(map(self.prior_transform, np.array(live_u))))
            if self.use_pool_logl:
                live_logl = np.array(
                    list(self.M(self.loglikelihood, np.array(live_v))))
            else:
                live_logl = np.array(
                    list(map(self.loglikelihood, np.array(live_v))))
            # Convert all `-np.inf` log-likelihoods to finite large numbers.
            # Necessary to keep estimators in our sampler from breaking.
            for i, logl in enumerate(live_logl):
                if not np.isfinite(logl):
                    if np.sign(logl) < 0:
                        live_logl[i] = -1e300
                    else:
                        raise ValueError("The log-likelihood ({0}) of live "
                                         "point {1} located at u={2} v={3} "
                                         " is invalid.".format(
                                             logl, i, live_u[i], live_v[i]))
            live_bound = np.zeros(nlive_new, dtype='int')
            live_it = np.zeros(nlive_new, dtype='int') + self.it
            live_nc = np.ones(nlive_new, dtype='int')
            self.ncall += nlive_new
            # Return live points in generator format.
            for i in range(nlive_new):
                yield (-i - 1, live_u[i], live_v[i], live_logl[i], live_nc[i],
                       live_it[i], 0, 0, self.eff)
        else:
            # If the lower bound doesn't encompass all base samples,
            # we need to create a uniform sample from the prior subject
            # to the likelihood boundary constraint
            subset = (saved_logl > logl_min)
            if subset.sum() == 0:
                raise RuntimeError(
                    'Could not find live points in the required logl interval')

            live_scale = saved_scale[subset][0]
            # set the scale based on the lowest point

            # we are weighting each point by 1/L_i * 1/W_i to ensure
            # uniform sampling within boundary volume
            cur_logwt = -saved_logl[subset] - saved_logwt[subset]
            cur_wt = np.exp(cur_logwt - cur_logwt.max())
            cur_wt = cur_wt / cur_wt.sum()
            # i normalize in linear space rather then using logsumexp
            # because cur_wt.sum() needs to be 1 for random.choice

            # we are now randomly sampling with weights
            # notice that since we are samplign without
            # replacement we aren't guaranteed to be able
            # to get nblive points
            # so we get min(nblive,subset.sum())
            # in that case the sample technically won't be
            # uniform
            subset = self.rstate.choice(np.nonzero(subset)[0],
                                        size=min(nblive, (cur_wt > 0).sum()),
                                        p=cur_wt,
                                        replace=False)
            cur_nblive = len(subset)

            live_u = saved_u[subset, :].copy()
            live_v = saved_v[subset, :].copy()
            live_logl = saved_logl[subset].copy()
            # Hack the internal sampler by overwriting the live points
            # and scale factor.
            self.sampler.nlive = cur_nblive
            self.sampler.live_u = live_u
            self.sampler.live_v = live_v
            self.sampler.live_logl = live_logl
            self.sampler.scale = live_scale

            # Trigger an update of the internal bounding distribution based
            # on the "new" set of live points.

            live_logl_min = min(live_logl)
            if self.sampler._beyond_unit_bound(live_logl_min):
                bound = self.sampler.update()
                if save_bounds:
                    self.sampler.bound.append(copy.deepcopy(bound))
                self.sampler.nbound += 1
                self.sampler.since_update = 0

            # Sample a new batch of `nlive_new` live points using the
            # internal sampler given the `logl_min` constraint.
            live_u = np.empty((nlive_new, self.npdim))
            live_v = np.empty((nlive_new, saved_v.shape[1]))
            live_logl = np.empty(nlive_new)
            live_bound = np.zeros(nlive_new, dtype='int')
            if self.sampler._beyond_unit_bound(live_logl_min):
                live_bound += self.sampler.nbound - 1
            live_it = np.empty(nlive_new, dtype='int')
            live_nc = np.empty(nlive_new, dtype='int')
            for i in range(nlive_new):
                (live_u[i], live_v[i], live_logl[i],
                 live_nc[i]) = self.sampler._new_point(logl_min)
                live_it[i] = self.it
                self.ncall += live_nc[i]
                # Return live points in generator format.
                yield (-i - 1, live_u[i], live_v[i], live_logl[i], live_nc[i],
                       live_it[i], live_bound[i], live_bound[i], self.eff)

        # Overwrite the previous set of live points in our internal sampler
        # with the new batch of points we just generated.
        self.sampler.nlive = nlive_new
        self.sampler.live_u = np.array(live_u)
        self.sampler.live_v = np.array(live_v)
        self.sampler.live_logl = np.array(live_logl)
        self.sampler.live_bound = np.array(live_bound)
        self.sampler.live_it = np.array(live_it)

        # Trigger an update of the internal bounding distribution (again).
        live_logl_min = min(live_logl)
        if self.sampler._beyond_unit_bound(live_logl_min):
            bound = self.sampler.update()  # vol / nlive_new)
            if save_bounds:
                self.sampler.bound.append(copy.deepcopy(bound))
            self.sampler.nbound += 1
            self.sampler.since_update = 0

        # Copy over bound reference.
        self.bound = self.sampler.bound

        # Update internal ln(prior volume)-based quantities
        if self.new_logl_min == -np.inf:
            bound_logvol = 0.
        else:
            vol_idx = np.argmin(
                abs(self.saved_run.D['logl'] - self.new_logl_min))
            bound_logvol = self.saved_run.D['logvol'][vol_idx]
        bound_dlv = math.log((nlive_new + 1.) / nlive_new)
        self.sampler.saved_run.D['logvol'][-1] = bound_logvol
        self.sampler.dlv = bound_dlv

        # Tell the sampler *not* to try and remove the previous addition of
        # live points. All the hacks above make the internal results
        # garbage anyways.
        self.sampler.added_live = False

        # Run the sampler internally as a generator until we hit
        # the lower likelihood threshold. Afterwards, we add in our remaining
        # live points *as if* we had terminated the run. This allows us to
        # sample past the original bounds "for free".

        for i in range(1):
            iterated_batch = False
            # To identify if the loop below was executed or not
            for it, results in enumerate(
                    self.sampler.sample(dlogz=dlogz_batch,
                                        logl_max=logl_max,
                                        maxiter=maxiter - nlive_new - 1,
                                        maxcall=maxcall - sum(live_nc),
                                        save_samples=False,
                                        save_bounds=save_bounds)):

                # Grab results.
                (worst, ustar, vstar, loglstar, logvol, _, logz, _, h, nc,
                 worst_it, boundidx, bounditer, _, _) = results

                # Save results.

                D = dict(id=worst,
                         u=ustar,
                         v=vstar,
                         logl=loglstar,
                         nc=nc,
                         it=worst_it,
                         n=nlive_new,
                         boundidx=boundidx,
                         bounditer=bounditer,
                         scale=self.sampler.scale)
                self.new_run.append(D)

                # Increment relevant counters.
                self.ncall += nc
                self.eff = 100. * self.it / self.ncall
                self.it += 1
                iterated_batch = True
                yield (worst, ustar, vstar, loglstar, nc, worst_it, boundidx,
                       bounditer, self.eff)

            if iterated_batch and loglstar < logl_max:
                warnings.warn('Warning. The maximum likelihood not reached '
                              'in the batch. '
                              'You may not have enough livepoints')

            for it, results in enumerate(self.sampler.add_live_points()):
                # Grab results.
                (worst, ustar, vstar, loglstar, logvol, _, logz, _, h, nc,
                 worst_it, boundidx, bounditer, _, _) = results

                # Save results.
                D = dict(id=worst,
                         u=ustar,
                         v=vstar,
                         logl=loglstar,
                         nc=live_nc[worst],
                         it=worst_it,
                         n=nlive_new - it,
                         boundidx=boundidx,
                         bounditer=bounditer,
                         scale=self.sampler.scale)
                self.new_run.append(D)

                # Increment relevant counters.
                self.eff = 100. * self.it / self.ncall
                self.it += 1

                yield (worst, ustar, vstar, loglstar, live_nc[worst], worst_it,
                       boundidx, bounditer, self.eff)

    def combine_runs(self):
        """ Merge the most recent run into the previous (combined) run by
        "stepping through" both runs simultaneously."""

        # Make sure we have a run to add.
        if len(self.new_run.D['id']) == 0:
            raise ValueError("No new samples are currently saved.")

        # Grab results from saved run.
        saved_d = {}
        new_d = {}

        for k in [
                'id', 'u', 'v', 'logl', 'nc', 'boundidx', 'it', 'bounditer',
                'n', 'scale'
        ]:
            saved_d[k] = np.array(self.saved_run.D[k])
            new_d[k] = np.array(self.new_run.D[k])

        saved_d['batch'] = np.array(self.saved_run.D['batch'])
        nsaved = len(saved_d['n'])

        new_d['id'] = new_d['id'] + max(saved_d['id']) + 1
        nnew = len(new_d['n'])
        llmin, llmax = self.new_logl_min, self.new_logl_max

        old_batch_bounds = self.saved_run.D['batch_bounds']
        old_batch_nlive = self.saved_run.D['batch_nlive']
        # Reset saved results.
        del self.saved_run
        self.saved_run = RunRecord()

        # Start our counters at the beginning of each set of dead points.
        idx_saved, idx_new = 0, 0  # start of our dead points
        logl_s, logl_n = saved_d['logl'][idx_saved], new_d['logl'][idx_new]
        nlive_s, nlive_n = saved_d['n'][idx_saved], new_d['n'][idx_new]

        # Iteratively walk through both set of samples to simulate
        # a combined run.
        ntot = nsaved + nnew
        logvol = 0.
        for i in range(ntot):
            if logl_s > self.new_logl_min:
                # If our saved samples are past the lower log-likelihood
                # bound, both runs are now "active" and should be used.
                nlive = nlive_s + nlive_n
            else:
                # If instead our collection of dead points are below
                # the bound, just use our collection of saved samples.
                nlive = nlive_s
            add_info = {}

            # Increment our position along depending on
            # which dead point (saved or new) is worse.
            if logl_s <= logl_n:
                add_info['batch'] = saved_d['batch'][idx_saved]
                add_source = saved_d
                add_idx = int(idx_saved)
                idx_saved += 1
            else:
                add_info['batch'] = self.batch + 1
                add_source = new_d
                add_idx = int(idx_new)
                idx_new += 1

            for k in [
                    'id', 'u', 'v', 'logl', 'nc', 'boundidx', 'it',
                    'bounditer', 'scale'
            ]:
                add_info[k] = add_source[k][add_idx]
            self.saved_run.append(add_info)

            # Save the number of live points and expected ln(volume).
            logvol -= math.log((nlive + 1.) / nlive)

            self.saved_run.D['n'].append(nlive)
            self.saved_run.D['logvol'].append(logvol)

            # Attempt to step along our samples. If we're out of samples,
            # set values to defaults.
            try:
                logl_s = saved_d['logl'][idx_saved]
                nlive_s = saved_d['n'][idx_saved]
            except IndexError:
                logl_s = np.inf
                nlive_s = 0
            try:
                logl_n = new_d['logl'][idx_new]
                nlive_n = new_d['n'][idx_new]
            except IndexError:
                logl_n = np.inf
                nlive_n = 0

        # Compute quantities of interest.
        h = 0.
        logz = -1.e300
        loglstar = -1.e300
        logzvar = 0.
        logvols_pad = np.concatenate(([0.], self.saved_run.D['logvol']))
        logdvols = logsumexp(a=np.c_[logvols_pad[:-1], logvols_pad[1:]],
                             axis=1,
                             b=np.c_[np.ones(ntot), -np.ones(ntot)])
        logdvols += math.log(0.5)
        dlvs = logvols_pad[:-1] - logvols_pad[1:]
        for i in range(ntot):
            loglstar_new = self.saved_run.D['logl'][i]
            logdvol, dlv = logdvols[i], dlvs[i]
            logwt = np.logaddexp(loglstar_new, loglstar) + logdvol
            logz_new = np.logaddexp(logz, logwt)
            lzterm = (
                math.exp(loglstar - logz_new + logdvol) * loglstar +
                math.exp(loglstar_new - logz_new + logdvol) * loglstar_new)
            h_new = (lzterm + math.exp(logz - logz_new) * (h + logz) -
                     logz_new)
            dh = h_new - h
            h = h_new
            logz = logz_new
            logzvar += 2. * dh * dlv
            loglstar = loglstar_new
            self.saved_run.D['logwt'].append(logwt)
            self.saved_run.D['logz'].append(logz)
            self.saved_run.D['logzvar'].append(logzvar)
            self.saved_run.D['h'].append(h)

        # Reset results.
        self.new_run = RunRecord()
        self.new_logl_min, self.new_logl_max = -np.inf, np.inf

        # Increment batch counter.
        self.batch += 1

        # Saved batch quantities.
        self.saved_run.D['batch_nlive'] = old_batch_nlive + [(max(new_d['n']))]
        self.saved_run.D['batch_bounds'] = old_batch_bounds + [(
            (llmin, llmax))]

    def _get_print_func(self, print_func, print_progress):
        pbar = None
        if print_func is None:
            if tqdm is None or not print_progress:
                print_func = print_fn
            else:
                pbar = tqdm.tqdm()
                print_func = partial(print_fn, pbar=pbar)
        return pbar, print_func

    def run_nested(self,
                   nlive_init=None,
                   maxiter_init=None,
                   maxcall_init=None,
                   dlogz_init=0.01,
                   logl_max_init=np.inf,
                   n_effective_init=np.inf,
                   nlive_batch=None,
                   wt_function=None,
                   wt_kwargs=None,
                   maxiter_batch=None,
                   maxcall_batch=None,
                   maxiter=None,
                   maxcall=None,
                   maxbatch=None,
                   n_effective=np.inf,
                   stop_function=None,
                   stop_kwargs=None,
                   use_stop=True,
                   save_bounds=True,
                   print_progress=True,
                   print_func=None,
                   live_points=None):
        """
        **The main dynamic nested sampling loop.** After an initial "baseline"
        run using a constant number of live points, dynamically allocates
        additional (nested) samples to optimize a specified weight function
        until a specified stopping criterion is reached.

        Parameters
        ----------
        nlive_init : int, optional
            The number of live points used during the initial ("baseline")
            nested sampling run. Default is the number provided at 
            initialization

        maxiter_init : int, optional
            Maximum number of iterations for the initial baseline nested
            sampling run. Iteration may stop earlier if the
            termination condition is reached. Default is `sys.maxsize`
            (no limit).

        maxcall_init : int, optional
            Maximum number of likelihood evaluations for the initial
            baseline nested sampling run. Iteration may stop earlier
            if the termination condition is reached. Default is `sys.maxsize`
            (no limit).

        dlogz_init : float, optional
            The baseline run will stop when the estimated contribution of the
            remaining prior volume to the total evidence falls below
            this threshold. Explicitly, the stopping criterion is
            `ln(z + z_est) - ln(z) < dlogz`, where `z` is the current
            evidence from all saved samples and `z_est` is the estimated
            contribution from the remaining volume. The default is
            `0.01`.

        logl_max_init : float, optional
            The baseline run will stop when the sampled ln(likelihood) exceeds
            this threshold. Default is no bound (`np.inf`).

        n_effective_init: int, optional
            Minimum number of effective posterior samples needed during the
            baseline run. If the estimated "effective sample size" (ESS)
            exceeds this number, sampling will terminate.
            Default is no ESS (`np.inf`).

        nlive_batch : int, optional
            The number of live points used when adding additional samples
            from a nested sampling run within each batch. Default is the
            number provided at init

        wt_function : func, optional
            A cost function that takes a :class:`Results` instance
            and returns a log-likelihood range over which a new batch of
            samples should be generated. The default function simply
            computes a weighted average of the posterior and evidence
            information content as::

                weight = pfrac * pweight + (1. - pfrac) * zweight

        wt_kwargs : dict, optional
            Extra arguments to be passed to the weight function.

        maxiter_batch : int, optional
            Maximum number of iterations for the nested
            sampling run within each batch. Iteration may stop earlier
            if the termination condition is reached. Default is `sys.maxsize`
            (no limit).

        maxcall_batch : int, optional
            Maximum number of likelihood evaluations for the nested
            sampling run within each batch. Iteration may stop earlier
            if the termination condition is reached. Default is `sys.maxsize`
            (no limit).

        maxiter : int, optional
            Maximum number of iterations allowed. Default is `sys.maxsize`
            (no limit).

        maxcall : int, optional
            Maximum number of likelihood evaluations allowed.
            Default is `sys.maxsize` (no limit).

        maxbatch : int, optional
            Maximum number of batches allowed. Default is `sys.maxsize`
            (no limit).

        n_effective: int, optional
            Minimum number of effective posterior samples needed during the
            entire run. If the estimated "effective sample size" (ESS)
            exceeds this number, sampling will terminate.
            Default is no ESS (`np.inf`).

        stop_function : func, optional
            A function that takes a :class:`Results` instance and
            returns a boolean indicating that we should terminate the run
            because we've collected enough samples.

        stop_kwargs : float, optional
            Extra arguments to be passed to the stopping function.

        use_stop : bool, optional
            Whether to evaluate our stopping function after each batch.
            Disabling this can improve performance if other stopping criteria
            such as :data:`maxcall` are already specified. Default is `True`.

        save_bounds : bool, optional
            Whether or not to save distributions used to bound
            the live points internally during dynamic live point allocation.
            Default is `True`.

        print_progress : bool, optional
            Whether to output a simple summary of the current run that
            updates each iteration. Default is `True`.

        print_func : function, optional
            A function that prints out the current state of the sampler.
            If not provided, the default :meth:`results.print_fn` is used.

        live_points : list of 3 `~numpy.ndarray` each with shape (nlive, ndim)
            A set of live points used to initialize the nested sampling run.
            Contains `live_u`, the coordinates on the unit cube, `live_v`, the
            transformed variables, and `live_logl`, the associated
            loglikelihoods. By default, if these are not provided the initial
            set of live points will be drawn from the unit `npdim`-cube.
            **WARNING: It is crucial that the initial set of live points have
            been sampled from the prior. Failure to provide a set of valid
            live points will result in biased results.**

        """

        # Initialize values.
        if maxcall is None:
            maxcall = sys.maxsize
        if maxiter is None:
            maxiter = sys.maxsize
        if maxiter_batch is None:
            maxiter_batch = sys.maxsize
        if maxcall_batch is None:
            maxcall_batch = sys.maxsize
        if maxbatch is None:
            maxbatch = sys.maxsize
        if maxiter_init is None:
            maxiter_init = sys.maxsize
        if maxcall_init is None:
            maxcall_init = sys.maxsize
        if wt_function is None:
            wt_function = weight_function
        if wt_kwargs is None:
            wt_kwargs = dict()
        if stop_function is None:
            stop_function = stopping_function
        if stop_kwargs is None:
            stop_kwargs = dict()

        nlive_init = nlive_init or self.nlive0
        nlive_batch = nlive_batch or self.nlive0

        # Run the main dynamic nested sampling loop.
        ncall = self.ncall
        niter = self.it - 1
        logl_bounds = (-np.inf, np.inf)
        maxcall_init = min(maxcall_init, maxcall)  # set max calls
        maxiter_init = min(maxiter_init, maxiter)  # set max iterations

        # Baseline run.
        pbar, print_func = self._get_print_func(print_func, print_progress)
        try:
            if not self.base:
                for results in self.sample_initial(
                        nlive=nlive_init,
                        dlogz=dlogz_init,
                        maxcall=maxcall_init,
                        maxiter=maxiter_init,
                        logl_max=logl_max_init,
                        n_effective=n_effective_init,
                        live_points=live_points):
                    (_, _, _, _, _, _, _, _, _, nc, _, _, _, _, _) = results

                    ncall += nc
                    niter += 1

                    # Print progress.
                    if print_progress:
                        print_func(results,
                                   niter,
                                   ncall,
                                   nbatch=0,
                                   dlogz=dlogz_init,
                                   logl_max=logl_max_init)

            # Add points in batches.
            for n in range(self.batch, maxbatch):
                # Update stopping criteria.
                res = self.results
                mcall = min(maxcall - ncall, maxcall_batch)
                miter = min(maxiter - niter, maxiter_batch)
                neff = self.n_effective
                if mcall > 0 and miter > 0 and neff < n_effective and use_stop:
                    if self.use_pool_stopfn:
                        M = self.M
                    else:
                        M = map
                    stop, stop_vals = stop_function(res,
                                                    stop_kwargs,
                                                    rstate=self.rstate,
                                                    M=M,
                                                    return_vals=True)
                    stop_post, stop_evid, stop_val = stop_vals
                else:
                    stop = False
                    stop_val = np.nan

                # If we have likelihood calls remaining, iterations remaining,
                # and we have failed to hit the minimum ESS, run our batch.
                if mcall > 0 and miter > 0 and neff < n_effective and not stop:
                    # Compute our sampling bounds using the provided
                    # weight function.
                    passback = self.add_batch(nlive=nlive_batch,
                                              wt_function=wt_function,
                                              wt_kwargs=wt_kwargs,
                                              maxiter=miter,
                                              maxcall=mcall,
                                              save_bounds=save_bounds,
                                              print_progress=print_progress,
                                              print_func=print_func,
                                              stop_val=stop_val)
                    ncall, niter, logl_bounds, results = passback
                elif logl_bounds[1] != np.inf:
                    # We ran at least one batch and now we're done!
                    if print_progress:
                        print_func(results,
                                   niter,
                                   ncall,
                                   nbatch=n,
                                   stop_val=stop_val,
                                   logl_min=logl_bounds[0],
                                   logl_max=logl_bounds[1])
                    break
                else:
                    # We didn't run a single batch but now we're done!
                    break
        finally:
            if pbar is not None:
                pbar.close()
            self.loglikelihood.history_save()

    def add_batch(self,
                  nlive=500,
                  wt_function=None,
                  wt_kwargs=None,
                  maxiter=None,
                  maxcall=None,
                  logl_bounds=None,
                  save_bounds=True,
                  print_progress=True,
                  print_func=None,
                  stop_val=None):
        """
        Allocate an additional batch of (nested) samples based on
        the combined set of previous samples using the specified
        weight function.

        Parameters
        ----------
        nlive : int, optional
            The number of live points used when adding additional samples
            in the batch. Default is `500`.

        wt_function : func, optional
            A cost function that takes a `Results` instance
            and returns a log-likelihood range over which a new batch of
            samples should be generated. The default function simply
            computes a weighted average of the posterior and evidence
            information content as::

                weight = pfrac * pweight + (1. - pfrac) * zweight

        wt_kwargs : dict, optional
            Extra arguments to be passed to the weight function.

        maxiter : int, optional
            Maximum number of iterations allowed. Default is `sys.maxsize`
            (no limit).

        maxcall : int, optional
            Maximum number of likelihood evaluations allowed.
            Default is `sys.maxsize` (no limit).

        logl_bounds : tuple of size (2,), optional
            The ln(likelihood) bounds used to bracket the run. If `None`,
            the provided `wt_function` will be used to determine the bounds
            (this is the default behavior).

        save_bounds : bool, optional
            Whether or not to save distributions used to bound
            the live points internally during dynamic live point allocations.
            Default is `True`.

        print_progress : bool, optional
            Whether to output a simple summary of the current run that
            updates each iteration. Default is `True`.

        print_func : function, optional
            A function that prints out the current state of the sampler.
            If not provided, the default :meth:`results.print_fn` is used.

        stop_val : float, optional
            The value of the stopping criteria to be passed to
            :meth:`print_func`. Used internally within :meth:`run_nested` to
            keep track of progress.

        """

        # Initialize values.
        if maxcall is None:
            maxcall = sys.maxsize
        if maxiter is None:
            maxiter = sys.maxsize
        if wt_function is None:
            wt_function = weight_function
        if wt_kwargs is None:
            wt_kwargs = dict()
        if stop_val is None:
            stop_val = np.nan

        # If we have either likelihood calls or iterations remaining,
        # add our new batch of live points.
        ncall, niter, n = self.ncall, self.it - 1, self.batch
        if maxcall > 0 and maxiter > 0:
            pbar, print_func = self._get_print_func(print_func, print_progress)
            try:
                # Compute our sampling bounds using the provided
                # weight function.
                res = self.results
                lnz, lnzerr = res.logz[-1], res.logzerr[-1]
                if logl_bounds is None:
                    logl_bounds = wt_function(res, wt_kwargs)
                for results in self.sample_batch(nlive_new=nlive,
                                                 logl_bounds=logl_bounds,
                                                 maxiter=maxiter,
                                                 maxcall=maxcall,
                                                 save_bounds=save_bounds):
                    (worst, ustar, vstar, loglstar, nc, worst_it, boundidx,
                     bounditer, eff) = results

                    # When initializing a batch (i.e. when `worst < 0`),
                    # don't increment our call counter or our current
                    # number of iterations.
                    if worst >= 0:
                        ncall += nc
                        niter += 1

                    # Reorganize results.
                    results = (worst, ustar, vstar, loglstar, np.nan, np.nan,
                               lnz, lnzerr**2, np.nan, nc, worst_it, boundidx,
                               bounditer, eff, np.nan)

                    # Print progress.
                    if print_progress:
                        print_func(results,
                                   niter,
                                   ncall,
                                   nbatch=n + 1,
                                   stop_val=stop_val,
                                   logl_min=logl_bounds[0],
                                   logl_max=logl_bounds[1])
            finally:
                if pbar is not None:
                    pbar.close()
                self.loglikelihood.history_save()

            # Combine batch with previous runs.
            self.combine_runs()
            # Pass back info.
            return ncall, niter, logl_bounds, results
        else:
            raise RuntimeError(
                'add_batch called with no leftover function calls'
                'or iterations')
