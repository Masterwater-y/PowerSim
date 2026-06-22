import math


def check_feasible(signatures, observation, tolerance=1e-6, algorithm="auto"):
    from .observations import observation_vectors

    counters = signatures["counters"]
    y, lo, hi, missing = observation_vectors(observation, counters)
    sigs = signatures["signatures"]
    matrix = [[float(sig["vector"].get(c, 0.0)) for sig in sigs] for c in counters]

    chosen = "scipy-linprog" if algorithm in ("auto", "scipy-linprog") and _has_scipy() else "projected-hinge-nnls"
    if chosen == "scipy-linprog":
        result = _solve_with_scipy(matrix, lo, hi, tolerance)
    else:
        result = _solve_pure_python(matrix, lo, hi, tolerance)

    weights = result["weights"]
    pred = _matvec(matrix, weights)
    rows = []
    max_norm = 0.0
    l2 = 0.0
    for name, obs_v, cil, cih, p in zip(counters, y, lo, hi, pred):
        violation, status = _interval_violation(p, cil, cih)
        scale = max(abs(obs_v), abs(cih - cil) if math.isfinite(cih) else 1.0, 1.0)
        norm = violation / scale
        max_norm = max(max_norm, abs(norm))
        l2 += norm * norm
        rows.append({
            "name": name,
            "observed": obs_v,
            "ci_low": cil,
            "ci_high": cih,
            "predicted": p,
            "violation": violation,
            "normalized_violation": norm,
            "status": status,
        })
    verdict = "feasible" if max_norm <= tolerance and result.get("success", False) else "infeasible"
    if result.get("numeric_status") == "uncertain":
        verdict = "uncertain"

    fit_weights = []
    for sig, w in zip(sigs, weights):
        if abs(w) > tolerance:
            fit_weights.append({"signature_id": sig["id"], "component": sig.get("component", "unknown"), "weight": w})
    fit_weights.sort(key=lambda x: abs(x["weight"]), reverse=True)
    return {
        "schema_version": "0.1",
        "verdict": verdict,
        "algorithm": {"name": chosen, "tolerance": tolerance, **result.get("algorithm", {})},
        "objective": {"max_normalized_violation": max_norm, "l2_normalized_violation": math.sqrt(l2)},
        "missing_observation_counters": missing,
        "fit": {"weights": fit_weights},
        "counters": rows,
    }


def _has_scipy():
    try:
        import scipy.optimize  # noqa: F401
        return True
    except Exception:
        return False


def _solve_with_scipy(S, lo, hi, tol):
    import numpy as np
    from scipy.optimize import linprog

    m = len(S)
    n = len(S[0]) if m else 0
    finite_hi = [h if math.isfinite(h) else 1e300 for h in hi]
    c = np.array([0.0] * n + [1.0] * m + [1.0] * m)
    A = []
    b = []
    for i in range(m):
        row = [0.0] * (n + 2 * m)
        for j in range(n):
            row[j] = S[i][j]
        row[n + i] = -1.0
        A.append(row)
        b.append(finite_hi[i])
    for i in range(m):
        row = [0.0] * (n + 2 * m)
        for j in range(n):
            row[j] = -S[i][j]
        row[n + m + i] = -1.0
        A.append(row)
        b.append(-lo[i])
    bounds = [(0.0, None)] * (n + 2 * m)
    res = linprog(c, A_ub=np.array(A), b_ub=np.array(b), bounds=bounds, method="highs")
    if not res.success:
        return {"weights": [0.0] * n, "success": False, "numeric_status": "uncertain", "algorithm": {"status": res.message}}
    slack_sum = float(sum(res.x[n:]))
    return {"weights": [float(x) for x in res.x[:n]], "success": slack_sum <= tol, "numeric_status": "ok", "algorithm": {"status": res.message, "slack_sum": slack_sum}}


def _solve_pure_python(S, lo, hi, tol):
    m = len(S)
    n = len(S[0]) if m else 0
    if n == 0:
        return {"weights": [], "success": False, "numeric_status": "uncertain", "algorithm": {"iterations": 0}}
    weights = [0.0] * n
    active = []
    for it in range(min(500, max(50, n * 20))):
        pred = _matvec(S, weights)
        residual = []
        done = True
        for p, l, h in zip(pred, lo, hi):
            v, _ = _interval_violation(p, l, h)
            scale = max(abs(l), abs(h) if math.isfinite(h) else 1.0, 1.0)
            residual.append(v / scale)
            if abs(residual[-1]) > tol:
                done = False
        if done:
            return {"weights": weights, "success": True, "numeric_status": "ok", "algorithm": {"iterations": it}}
        best_j, best_score = None, 0.0
        for j in range(n):
            if j in active:
                continue
            score = -sum(S[i][j] * residual[i] for i in range(m))
            if score > best_score:
                best_j, best_score = j, score
        if best_j is None:
            break
        active.append(best_j)
        target = [(lo[i] + (hi[i] if math.isfinite(hi[i]) else lo[i])) / 2.0 for i in range(m)]
        w_active = _least_squares_nonnegative([[S[i][j] for j in active] for i in range(m)], target)
        weights = [0.0] * n
        for j, w in zip(active, w_active):
            weights[j] = max(0.0, w)
        active = [j for j in active if weights[j] > tol]
    return {"weights": weights, "success": False, "numeric_status": "ok", "algorithm": {"iterations": len(active)}}


def _least_squares_nonnegative(A, b):
    if not A or not A[0]:
        return []
    m, n = len(A), len(A[0])
    ata = [[sum(A[i][j] * A[i][k] for i in range(m)) for k in range(n)] for j in range(n)]
    atb = [sum(A[i][j] * b[i] for i in range(m)) for j in range(n)]
    for i in range(n):
        ata[i][i] += 1e-12
    x = _gaussian_solve(ata, atb)
    return [max(0.0, v) for v in x]


def _gaussian_solve(A, b):
    n = len(b)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-18:
            continue
        M[col], M[pivot] = M[pivot], M[col]
        div = M[col][col]
        M[col] = [x / div for x in M[col]]
        for r in range(n):
            if r == col:
                continue
            factor = M[r][col]
            M[r] = [rv - factor * cv for rv, cv in zip(M[r], M[col])]
    return [M[i][-1] for i in range(n)]


def _matvec(S, w):
    return [sum(row[j] * w[j] for j in range(len(w))) for row in S]


def _interval_violation(v, lo, hi):
    if v < lo:
        return v - lo, "below_ci"
    if math.isfinite(hi) and v > hi:
        return v - hi, "above_ci"
    return 0.0, "inside"

