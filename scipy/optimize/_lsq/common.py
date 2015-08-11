"""Functions used by least-squares algorithms."""
from math import copysign

import numpy as np
from numpy.linalg import norm

from scipy.linalg import cho_factor, cho_solve, LinAlgError
from scipy.sparse import issparse
from scipy.sparse.linalg import LinearOperator, aslinearoperator


EPS = np.finfo(float).eps


# Functions related to a trust-region problem.


def intersect_trust_region(x, s, Delta):
    """Find the intersection of a line with a spherical bound of a trust
    region.

    This function just solves the quadratic equation with respect to t
    ||(x + s*t)||**2 = Delta**2.

    Returns
    -------
    t_neg, t_pos : tuple of float
        Negative and positive roots.

    Raises
    ------
    ValueError
        If `s` is zero or `x` is not within the trust region.
    """
    a = np.dot(s, s)
    if a == 0:
        raise ValueError("`s` is zero.")

    b = np.dot(x, s)

    c = np.dot(x, x) - Delta**2
    if c > 0:
        raise ValueError("`x` is not within the trust region.")

    d = np.sqrt(b*b - a*c)  # Root from one forth of the discriminant.

    # Computations below avoid loss of significance, see "Numerical Recipes".
    q = -(b + copysign(d, b))
    t1 = q / a
    t2 = c / q

    if t1 < t2:
        return t1, t2
    else:
        return t2, t1


def _phi_and_derivative(alpha, suf, s, Delta):
    """Used by solve_lsq_trust_region."""
    denom = s**2 + alpha
    p_norm = norm(suf / denom)
    phi = p_norm - Delta
    phi_prime = -np.sum(suf ** 2 / denom**3) / p_norm
    return phi, phi_prime


def solve_lsq_trust_region(n, m, uf, s, V, Delta, initial_alpha=None,
                           rtol=0.01, max_iter=10):
    """Solve a trust-region problem arising in least-squares minimization by
    MINPACK approach.

    This function implements a method described by J. J. More [1]_, but it
    relies on SVD of Jacobian. Before running this function compute:
    ``U, s, VT = svd(J, full_matrices=False)``.

    Parameters
    ----------
    n : int
        Number of variables.
    m : int
        Number of residuals.
    uf : ndarray
        Should be computed as U.T.dot(f).
    s : ndarray
        Singular values of J.
    V : ndarray
        Transpose of VT.
    Delta : float
        Radius of a trust region.
    initial_alpha : float, optional
        Initial guess for alpha, which might be available from a previous
        iteration.
    rtol : float, optional
        Stopping tolerance for the root-finding procedure. Namely, the
        solution p will satisfy ``abs(norm(p) - Delta) < rtol * Delta``.
    max_iter : int, optional
        Maximum allowed number of iterations for the root-finding procedure.

    Returns
    -------
    p : ndarray, shape (n,)
        Found solution of a trust-region problem.
    alpha : float
        Positive value such that (J.T*J + alpha*I)*p = -J.T*f.
        Sometimes called Levenberg-Marquardt parameter.
    n_iter : int
        Number of iterations made by root-finding procedure. Zero means
        that Gauss-Newton step was selected as the solution.

    References
    ----------
    .. [1] More, J. J., "The Levenberg-Marquardt Algorithm: Implementation
           and Theory," Numerical Analysis, ed. G. A. Watson, Lecture Notes
           in Mathematics 630, Springer Verlag, pp. 105-116, 1977.
    """
    suf = s * uf

    # Check if J has full rank and try Gauss-Newton step.
    if m >= n:
        threshold = EPS * m * s[0]
        full_rank = s[-1] > threshold
    else:
        full_rank = False

    if full_rank:
        p = -V.dot(uf / s)
        if norm(p) <= Delta:
            return p, 0.0, 0

    alpha_upper = norm(suf) / Delta

    if full_rank:
        phi, phi_prime = _phi_and_derivative(0.0, suf, s, Delta)
        alpha_lower = -phi / phi_prime
    else:
        alpha_lower = 0.0

    if initial_alpha is None or not full_rank and initial_alpha == 0:
        alpha = max(0.001 * alpha_upper, (alpha_lower * alpha_upper)**0.5)
    else:
        alpha = initial_alpha

    for it in range(max_iter):
        if alpha < alpha_lower or alpha > alpha_upper:
            alpha = max(0.001 * alpha_upper, (alpha_lower * alpha_upper)**0.5)

        phi, phi_prime = _phi_and_derivative(alpha, suf, s, Delta)

        if phi < 0:
            alpha_upper = alpha

        ratio = phi / phi_prime
        alpha_lower = max(alpha_lower, alpha - ratio)
        alpha -= (phi + Delta) * ratio / Delta

        if np.abs(phi) < rtol * Delta:
            break

    p = -V.dot(suf / (s**2 + alpha))
    p *= Delta / norm(p)

    return p, alpha, it + 1


def solve_trust_region_2d(B, g, Delta):
    """Solve a 2-dimensional general trust-region problem.

    The problem is reformulated as a 4-th order algebraic equation,
    the solution of which is found by numpy.roots.

    Parameters
    ----------
    B : ndarray, shape (2, 2)
        Symmetric matrix, defines a quadratic term of the function.
    g : ndarray, shape (2,)
        Defines a linear term of the function.
    Delta : float
        Trust-region radius.

    Returns
    -------
    p : ndarray, shape (2,)
        Found solution.
    newton_step : bool
        Whether the returned solution is the Newton step which lies within
        the trust region.
    """
    try:
        R, lower = cho_factor(B)
        p = -cho_solve((R, lower), g)
        if np.dot(p, p) <= Delta**2:
            return p, True
    except LinAlgError:
        pass

    a = B[0, 0] * Delta**2
    b = B[0, 1] * Delta**2
    c = B[1, 1] * Delta**2

    d = g[0] * Delta
    f = g[1] * Delta

    coeffs = np.array(
        [-b + d, 2 * (a - c + f), 6 * b, 2 * (-a + c + f), -b - d])
    t = np.roots(coeffs)  # Can handle leading zeros.
    t = np.real(t[np.isreal(t)])

    p = Delta * np.vstack((2 * t / (1 + t**2), (1 - t**2) / (1 + t**2)))
    value = 0.5 * np.sum(p * B.dot(p), axis=0) + np.dot(g, p)
    i = np.argmin(value)
    p = p[:, i]

    return p, False


def update_tr_radius(Delta, actual_reduction, predicted_reduction,
                     step_norm, bound_hit):
    if predicted_reduction > 0:
        ratio = actual_reduction / predicted_reduction
    else:
        ratio = 0

    if ratio < 0.25:
        Delta = 0.25 * step_norm
    elif ratio > 0.75 and bound_hit:
        Delta *= 2.0

    return Delta, ratio


# Construction and minimization of quadratic functions.


def build_quadratic_1d(J, g, s, diag=None, s0=None):
    """Compute coefficients of a 1-d quadratic function for the line search
    from a multidimensional quadratic function.

    The function is given as follows:
    ::

        f(t) = 0.5 * (s0 + s*t).T * (J.T*J + diag) * (s0 + s*t) +
               g.T * (s0 + s*t)

    Parameters
    ----------
    J : ndarray, sparse matrix or LinearOperator shape (m, n)
        Jacobian matrix, affects the quadratic term.
    g : ndarray, shape (n,)
        Gradient, defines the linear term.
    s : ndarray, shape (n,)
        Direction of search.
    diag : None or ndarray with shape (n,), optional
        Addition diagonal part, affects the quadratic term.
        If None, assumed to be 0.
    s0 : None or ndarray with shape (n,), optional
        Initial point. If None, assumed to be 0.

    Returns
    -------
    a : float
        Coefficient for t**2.
    b : float
        Coefficient for t.
    c : float
        Free term. Returned only if `s0` is provided.
    """
    v = J.dot(s)
    a = np.dot(v, v)
    if diag is not None:
        a += np.dot(s * diag, s)
    a *= 0.5

    b = np.dot(g, s)

    if s0 is not None:
        u = J.dot(s0)
        b += np.dot(u, v)
        c = 0.5 * np.dot(u, u) + np.dot(g, s0)
        if diag is not None:
            b += np.dot(s0 * diag, s)
            c += 0.5 * np.dot(s0 * diag, s0)
        return a, b, c
    else:
        return a, b


def minimize_quadratic_1d(a, b, lb, ub, c=0):
    """Minimize a 1-d quadratic function subject to bounds.

    The free term `c` is 0 by default. Bounds must be finite.

    Returns
    -------
    t : float
        Minimum point.
    y : float
        Minimum value.
    """
    t = [lb, ub]
    if a != 0:
        extremum = -0.5 * b / a
        if lb < extremum < ub:
            t.append(extremum)
    t = np.asarray(t)
    y = a * t**2 + b * t + c
    min_index = np.argmin(y)
    return t[min_index], y[min_index]


def evaluate_quadratic(J, g, s, diag=None):
    """Compute values of a quadratic function arising in least-squares.

    The function is 0.5 * s.T * (J.T * J + diag) * s + g.T * s.

    Parameters
    ----------
    J : ndarray, sparse matrix or LinearOperator, shape (m, n)
        Jacobian matrix, affects the quadratic term.
    g : ndarray, shape (n,)
        Gradient, defines the linear term.
    s : ndarray, shape (k, n) or (n,)
        Array containing steps as rows.
    diag : ndarray, shape (n,), optional
        Addition diagonal part, affects the quadratic term.
        If None, assumed to be 0.

    Returns
    -------
    values : ndarray with shape (k,) or float
        Values of the function. If `s` was 2-dimensional then ndarray is
        returned, otherwise float is returned.
    """
    if s.ndim == 1:
        Js = J.dot(s)
        q = np.dot(Js, Js)
        if diag is not None:
            q += np.dot(s * diag, s)
    else:
        Js = J.dot(s.T)
        q = np.sum(Js**2, axis=0)
        if diag is not None:
            q += np.sum(diag * s**2, axis=1)

    l = np.dot(s, g)

    return 0.5 * q + l


# Utility functions to work with bound constraints.


def in_bounds(x, lb, ub):
    """Check if a point lies within bounds."""
    return np.all((x >= lb) & (x <= ub))


def step_size_to_bound(x, s, lb, ub):
    """Compute a min_step size required to reach a bound.

    The function computes a positive scalar t, such that x + s * t is on
    the bound.

    Returns
    -------
    step : float
        Computed step. Non-negative value.
    hits : ndarray of int with shape of x
        Each element indicates whether a corresponding variable reaches the
        bound:

             *  0 - the bound was not hit.
             * -1 - the lower bound was hit.
             *  1 - the upper bound was hit.
    """
    non_zero = np.nonzero(s)
    s_non_zero = s[non_zero]
    steps = np.empty_like(x)
    steps.fill(np.inf)
    with np.errstate(over='ignore'):
        steps[non_zero] = np.maximum((lb - x)[non_zero] / s_non_zero,
                                     (ub - x)[non_zero] / s_non_zero)
    min_step = np.min(steps)
    return min_step, np.equal(steps, min_step) * np.sign(s).astype(int)


def find_active_constraints(x, lb, ub, rtol=1e-10):
    """Determine which constraints are active in a given point.

    The threshold is computed using `rtol` and the absolute value of the
    closest bound.

    Returns
    ------
    active : ndarray of int with shape of x
        Each component shows whether the corresponding constraint is active:

             *  0 - a constraint is not active.
             * -1 - a lower bound is active.
             *  1 - a upper bound is active.
    """
    active = np.zeros_like(x, dtype=int)

    if rtol == 0:
        active[x <= lb] = -1
        active[x >= ub] = 1
        return active

    lower_dist = x - lb
    upper_dist = ub - x

    lower_threshold = rtol * np.maximum(1, np.abs(lb))
    upper_threshold = rtol * np.maximum(1, np.abs(ub))

    lower_active = (np.isfinite(lb) &
                    (lower_dist <= np.minimum(upper_dist, lower_threshold)))
    active[lower_active] = -1

    upper_active = (np.isfinite(ub) &
                    (upper_dist <= np.minimum(lower_dist, upper_threshold)))
    active[upper_active] = 1

    return active


def make_strictly_feasible(x, lb, ub, rstep=1e-10):
    """Shift a point to the interior of a feasible region.

    Each element of the returned vector is at least at a relative distance
    `rstep` from the closest bound. If ``rstep=0`` then `np.nextafter` is used.
    """
    x_new = x.copy()

    active = find_active_constraints(x, lb, ub, rstep)
    lower_mask = np.equal(active, -1)
    upper_mask = np.equal(active, 1)

    if rstep == 0:
        x_new[lower_mask] = np.nextafter(lb[lower_mask], ub[lower_mask])
        x_new[upper_mask] = np.nextafter(ub[upper_mask], lb[upper_mask])
    else:
        x_new[lower_mask] = (lb[lower_mask] +
                             rstep * np.maximum(1, np.abs(lb[lower_mask])))
        x_new[upper_mask] = (ub[upper_mask] -
                             rstep * np.maximum(1, np.abs(ub[upper_mask])))

    tight_bounds = (x_new < lb) | (x_new > ub)
    x_new[tight_bounds] = 0.5 * (lb[tight_bounds] + ub[tight_bounds])

    return x_new

 
def scaling_vector(x, g, lb, ub):
    """Compute a scaling vector and its derivatives as described in papers
    of Coleman and Li [1]_.

    Components of a vector v are defined as follows:
    ::

               | ub[i] - x[i], if g[i] < 0 and ub[i] < np.inf
        v[i] = | x[i] - lb[i], if g[i] > 0 and lb[i] > -np.inf
               | 1,           otherwise

    According to this definition v[i] >= 0 for all i. It differs from the
    definition in paper [1]_ (eq. (2.2)), where the absolute value of v is
    used. Both definitions are equivalent down the line.

    Derivatives of v with respect to x take value 1, -1 or 0 depending on a
    case.

    Returns
    -------
    v : ndarray with shape of x
        Scaling vector.
    dv : ndarray with shape of x
        Derivatives of v[i] with respect to x[i], diagonal elements of v's
        Jacobian.

    References
    ----------
    .. [1] Branch, M.A., T.F. Coleman, and Y. Li, "A Subspace, Interior,
           and Conjugate Gradient Method for Large-Scale Bound-Constrained
           Minimization Problems," SIAM Journal on Scientific Computing,
           Vol. 21, Number 1, pp 1-23, 1999.
    """
    v = np.ones_like(x)
    dv = np.zeros_like(x)

    mask = (g < 0) & np.isfinite(ub)
    v[mask] = ub[mask] - x[mask]
    dv[mask] = -1

    mask = (g > 0) & np.isfinite(lb)
    v[mask] = x[mask] - lb[mask]
    dv[mask] = 1

    return v, dv


# Functions to display algorithm's progress.


def print_header():
    print("{0:^15}{1:^15}{2:^15}{3:^15}{4:^15}{5:^15}"
          .format("Iteration", "Total nfev", "Cost", "Cost reduction",
                  "Step norm", "Optimality"))


def print_iteration(iteration, nfev, cost, cost_reduction, step_norm,
                    optimality):
    if cost_reduction is None:
        cost_reduction = " " * 15
    else:
        cost_reduction = "{0:^15.2e}".format(cost_reduction)
    if step_norm is None:
        step_norm = " " * 15
    else:
        step_norm = "{0:^15.2e}".format(step_norm)
    print("{0:^15}{1:^15}{2:^15.4e}{3}{4}{5:^15.2e}"
          .format(iteration, nfev, cost, cost_reduction,
                  step_norm, optimality))


# Simple helper functions.


def compute_grad(J, f):
    if isinstance(J, LinearOperator):
        return J.rmatvec(f)
    else:
        return J.T.dot(f)


def compute_jac_scaling(J, old_scale=None):
    if issparse(J):
        scale = np.asarray(J.power(2).sum(axis=0)).ravel()**0.5
    else:
        scale = np.sum(J**2, axis=0)**0.5

    if old_scale is None:
        scale[scale == 0] = 1
    else:
        scale = np.maximum(scale, old_scale)

    return scale, 1 / scale


def left_multiplied_operator(J, d):
    """Return diag(d) J as LinearOperator."""
    J = aslinearoperator(J)

    def matvec(x):
        return d * J.matvec(x)

    def matmat(X):
        return d * J.matmat(X)

    def rmatvec(x):
        return J.rmatvec(x.ravel() * d)

    return LinearOperator(J.shape, matvec=matvec, matmat=matmat,
                          rmatvec=rmatvec)


def right_multiplied_operator(J, d):
    """Return J diag(d) as LinearOperator."""
    J = aslinearoperator(J)

    def matvec(x):
        return J.matvec(np.ravel(x) * d)

    def matmat(X):
        return J.matmat(X * d[:, np.newaxis])

    def rmatvec(x):
        return d * J.rmatvec(x)

    return LinearOperator(J.shape, matvec=matvec, matmat=matmat,
                          rmatvec=rmatvec)


def regularized_lsq_operator(J, diag):
    """Return a matrix arising in a regularized least-squares problem as
    linear a operator.

    The matrix is
        [ J ]
        [ D ]
    where D is diagonal matrix with elements from `diag`.
    """
    J = aslinearoperator(J)
    m, n = J.shape

    def matvec(x):
        return np.hstack((J.matvec(x), diag * x))

    def rmatvec(x):
        x1 = x[:m]
        x2 = x[m:]
        return J.rmatvec(x1) + diag * x2

    return LinearOperator((m + n, n), matvec=matvec, rmatvec=rmatvec)


def right_multiply(J, d, copy=True):
    """Compute J diag(d).

    If `copy` is False, `J` is modified in place (unless being LinearOperator).
    """
    if copy and not isinstance(J, LinearOperator):
        J = J.copy()

    if issparse(J):
        J.data *= d.take(J.indices, mode='clip')  # scikit-learn recipe.
    elif isinstance(J, LinearOperator):
        J = right_multiplied_operator(J, d)
    else:
        J *= d

    return J


def left_multiply(J, d, copy=True):
    """Compute diag(d) J.

    If `copy` is False, `J` is modified in place (unless being LinearOperator).
    """
    if copy and not isinstance(J, LinearOperator):
        J = J.copy()

    if issparse(J):
        J.data *= np.repeat(d, np.diff(J.indptr))  # scikit-learn recipe.
    elif isinstance(J, LinearOperator):
        J = left_multiplied_operator(J, d)
    else:
        J *= d[:, np.newaxis]

    return J


def check_termination(dF, F, dx_norm, x_norm, ratio, ftol, xtol):
    ftol_satisfied = dF < ftol * F and ratio > 0.25
    xtol_satisfied = dx_norm < xtol * (xtol + x_norm)

    if ftol_satisfied and xtol_satisfied:
        return 4
    elif ftol_satisfied:
        return 2
    elif xtol_satisfied:
        return 3
    else:
        return None


def correct_by_loss(J, f, rho):
    """Correct J and f to make an iteration correspond to a robust loss
    function.

    Arrays are modified in place.
    """
    J_scale = rho[1] + 2 * rho[2] * f**2
    J_scale[J_scale < EPS] = EPS
    J_scale **= 0.5

    f *= rho[1] / J_scale

    return left_multiply(J, J_scale, copy=False), f
