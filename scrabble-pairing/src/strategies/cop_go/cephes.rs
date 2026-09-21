//! The inverse regularized incomplete beta function, for upstream's
//! Clopper–Pearson interval: gonum's `mathext.InvRegIncBeta`, which is its port
//! of Cephes `incbi`/`incbet`/`ndtri` (Stephen L. Moshier, 1984–1996). Ported
//! line for line from gonum v0.17.0 — the same algorithm, so the re-simulation
//! loop stops where upstream's does. Elementary functions come from the `libm`
//! crate rather than the platform, so the answer does not vary by machine.
//!
//! Cephes' `goto`s become the `Step` state machine in `incbi`.

// The coefficients are Cephes' own, digit for digit.
#![allow(clippy::excessive_precision)]

use libm::{exp, fabs, lgamma, log, pow, sqrt};

const MACH_EP: f64 = 1.0 / (1u64 << 53) as f64;
const MAX_LOG: f64 = 1024.0 * std::f64::consts::LN_2;
const MIN_LOG: f64 = -1075.0 * std::f64::consts::LN_2;
const MAX_GAM: f64 = 171.624376956302725;
const BIG: f64 = 4.503599627370496e15;
const BIGINV: f64 = 2.22044604925031308085e-16;

/// `distuv.Beta{Alpha: a, Beta: b}.Quantile(p)`.
pub fn beta_quantile(a: f64, b: f64, p: f64) -> f64 {
    assert!((0.0..=1.0).contains(&p), "beta quantile: p out of range");
    incbi(a, b, p)
}

fn lbeta(a: f64, b: f64) -> f64 {
    if a.is_infinite() && a > 0.0 || b.is_infinite() && b > 0.0 {
        return f64::NAN;
    }
    if (a == 0.0 && b == 0.0) || a < 0.0 || b < 0.0 || a.is_nan() || b.is_nan() {
        return f64::NAN;
    }
    if a == 0.0 || b == 0.0 {
        return f64::INFINITY;
    }
    lgamma(a) + lgamma(b) - lgamma(a + b)
}

fn beta(a: f64, b: f64) -> f64 {
    exp(lbeta(a, b))
}

fn polevl(x: f64, coef: &[f64], n: usize) -> f64 {
    let mut ans = coef[0];
    for c in &coef[1..=n] {
        ans = ans * x + c;
    }
    ans
}

fn p1evl(x: f64, coef: &[f64], n: usize) -> f64 {
    let mut ans = x + coef[0];
    for c in &coef[1..n] {
        ans = ans * x + c;
    }
    ans
}

const S2PI: f64 = 2.50662827463100050242e0;
const P0: [f64; 5] = [
    -5.99633501014107895267e1,
    9.80010754185999661536e1,
    -5.66762857469070293439e1,
    1.39312609387279679503e1,
    -1.23916583867381258016e0,
];
const Q0: [f64; 8] = [
    1.95448858338141759834e0,
    4.67627912898881538453e0,
    8.63602421390890590575e1,
    -2.25462687854119370527e2,
    2.00260212380060660359e2,
    -8.20372256168333339912e1,
    1.59056225126211695515e1,
    -1.18331621121330003142e0,
];
const P1: [f64; 9] = [
    4.05544892305962419923e0,
    3.15251094599893866154e1,
    5.71628192246421288162e1,
    4.40805073893200834700e1,
    1.46849561928858024014e1,
    2.18663306850790267539e0,
    -1.40256079171354495875e-1,
    -3.50424626827848203418e-2,
    -8.57456785154685413611e-4,
];
const Q1: [f64; 8] = [
    1.57799883256466749731e1,
    4.53907635128879210584e1,
    4.13172038254672030440e1,
    1.50425385692907503408e1,
    2.50464946208309415979e0,
    -1.42182922854787788574e-1,
    -3.80806407691578277194e-2,
    -9.33259480895457427372e-4,
];
const P2: [f64; 9] = [
    3.23774891776946035970e0,
    6.91522889068984211695e0,
    3.93881025292474443415e0,
    1.33303460815807542389e0,
    2.01485389549179081538e-1,
    1.23716634817820021358e-2,
    3.01581553508235416007e-4,
    2.65806974686737550832e-6,
    6.23974539184983293730e-9,
];
const Q2: [f64; 8] = [
    6.02427039364742014255e0,
    3.67983563856160859403e0,
    1.37702099489081330271e0,
    2.16236993594496635890e-1,
    1.34204006088543189037e-2,
    3.28014464682127739104e-4,
    2.89247864745380683936e-6,
    6.79019408009981274425e-9,
];

/// Inverse of the standard normal CDF.
fn ndtri(y0: f64) -> f64 {
    assert!((0.0..=1.0).contains(&y0), "ndtri: parameter out of range");
    if y0 == 0.0 {
        return f64::NEG_INFINITY;
    }
    if y0 == 1.0 {
        return f64::INFINITY;
    }
    let mut code = true;
    let mut y = y0;
    if y > 1.0 - 0.13533528323661269189 {
        y = 1.0 - y;
        code = false;
    }
    if y > 0.13533528323661269189 {
        y -= 0.5;
        let y2 = y * y;
        let x = y + y * (y2 * polevl(y2, &P0, 4) / p1evl(y2, &Q0, 8));
        return x * S2PI;
    }
    let x = sqrt(-2.0 * log(y));
    let x0 = x - log(x) / x;
    let z = 1.0 / x;
    let x1 = if x < 8.0 {
        z * polevl(z, &P1, 8) / p1evl(z, &Q1, 8)
    } else {
        z * polevl(z, &P2, 8) / p1evl(z, &Q2, 8)
    };
    let x = x0 - x1;
    if code {
        -x
    } else {
        x
    }
}

fn transform_t(t: f64, flag: bool) -> f64 {
    if !flag {
        t
    } else if t <= MACH_EP {
        1.0 - MACH_EP
    } else {
        1.0 - t
    }
}

/// The regularized incomplete beta function.
fn incbet(aa: f64, bb: f64, xx: f64) -> f64 {
    assert!(aa > 0.0 && bb > 0.0, "incbet: parameter out of range");
    if xx <= 0.0 || xx >= 1.0 {
        if xx == 0.0 {
            return 0.0;
        }
        if xx == 1.0 {
            return 1.0;
        }
        panic!("incbet: parameter out of range");
    }
    if bb * xx <= 1.0 && xx <= 0.95 {
        return transform_t(pseries(aa, bb, xx), false);
    }
    let mut w = 1.0 - xx;
    let (flag, a, b, xc, x) = if xx > aa / (aa + bb) {
        (true, bb, aa, xx, w)
    } else {
        (false, aa, bb, w, xx)
    };
    if flag && b * x <= 1.0 && x <= 0.95 {
        return transform_t(pseries(a, b, x), flag);
    }
    let y = x * (a + b - 2.0) - (a - 1.0);
    w = if y < 0.0 { incbcf(a, b, x) } else { incbd(a, b, x) / xc };
    let mut y = a * log(x);
    let mut t = b * log(xc);
    if a + b < MAX_GAM && fabs(y) < MAX_LOG && fabs(t) < MAX_LOG {
        t = pow(xc, b);
        t *= pow(x, a);
        t /= a;
        t *= w;
        t *= 1.0 / beta(a, b);
        return transform_t(t, flag);
    }
    y += t - lbeta(a, b);
    y += log(w / a);
    t = if y < MIN_LOG { 0.0 } else { exp(y) };
    transform_t(t, flag)
}

/// Continued fraction expansion #1 (`incbcf`) and #2 (`incbd`) share one shape;
/// they differ in their starting terms, how `k2`/`k6` step, and the variable.
#[allow(clippy::too_many_arguments)]
fn continued_fraction(
    k: [f64; 8],
    k2_step: f64,
    k6_step: f64,
    z: f64,
) -> f64 {
    let [mut k1, mut k2, mut k3, mut k4, mut k5, mut k6, mut k7, mut k8] = k;
    let (mut pkm2, mut qkm2, mut pkm1, mut qkm1) = (0.0, 1.0, 1.0, 1.0);
    let mut ans = 1.0;
    let mut r = 1.0;
    let thresh = 3.0 * MACH_EP;
    for _ in 0..=300 {
        let mut xk = -(z * k1 * k2) / (k3 * k4);
        let mut pk = pkm1 + pkm2 * xk;
        let mut qk = qkm1 + qkm2 * xk;
        pkm2 = pkm1;
        pkm1 = pk;
        qkm2 = qkm1;
        qkm1 = qk;
        xk = (z * k5 * k6) / (k7 * k8);
        pk = pkm1 + pkm2 * xk;
        qk = qkm1 + qkm2 * xk;
        pkm2 = pkm1;
        pkm1 = pk;
        qkm2 = qkm1;
        qkm1 = qk;
        if qk != 0.0 {
            r = pk / qk;
        }
        let t = if r != 0.0 {
            let t = fabs((ans - r) / r);
            ans = r;
            t
        } else {
            1.0
        };
        if t < thresh {
            return ans;
        }
        k1 += 1.0;
        k2 += k2_step;
        k3 += 2.0;
        k4 += 2.0;
        k5 += 1.0;
        k6 += k6_step;
        k7 += 2.0;
        k8 += 2.0;
        if fabs(qk) + fabs(pk) > BIG {
            pkm2 *= BIGINV;
            pkm1 *= BIGINV;
            qkm2 *= BIGINV;
            qkm1 *= BIGINV;
        }
        if fabs(qk) < BIGINV || fabs(pk) < BIGINV {
            pkm2 *= BIG;
            pkm1 *= BIG;
            qkm2 *= BIG;
            qkm1 *= BIG;
        }
    }
    ans
}

fn incbcf(a: f64, b: f64, x: f64) -> f64 {
    continued_fraction([a, a + b, a, a + 1.0, 1.0, b - 1.0, a + 1.0, a + 2.0], 1.0, -1.0, x)
}

fn incbd(a: f64, b: f64, x: f64) -> f64 {
    let z = x / (1.0 - x);
    continued_fraction([a, b - 1.0, a, a + 1.0, 1.0, a + b, a + 1.0, a + 2.0], -1.0, 1.0, z)
}

/// Power series for the incomplete beta integral, for small `b*x`.
fn pseries(a: f64, b: f64, x: f64) -> f64 {
    let ai = 1.0 / a;
    let mut u = (1.0 - b) * x;
    let mut v = u / (a + 1.0);
    let t1 = v;
    let mut t = u;
    let mut n = 2.0;
    let mut s = 0.0;
    let z = MACH_EP * ai;
    while fabs(v) > z {
        u = (n - b) * x / n;
        t *= u;
        v = t / (a + n);
        s += v;
        n += 1.0;
    }
    s += t1;
    s += ai;
    u = a * log(x);
    if a + b < MAX_GAM && fabs(u) < MAX_LOG {
        t = 1.0 / beta(a, b);
        s * t * pow(x, a)
    } else {
        t = -lbeta(a, b) + u + log(s);
        if t < MIN_LOG {
            0.0
        } else {
            exp(t)
        }
    }
}

enum Step {
    Ihalve,
    Newt,
    Done,
}

/// Inverse of the regularized incomplete beta function (Cephes `incbi`).
fn incbi(aa: f64, bb: f64, yy0: f64) -> f64 {
    if yy0 <= 0.0 {
        return 0.0;
    }
    if yy0 >= 1.0 {
        return 1.0;
    }
    let (mut x0, mut yl, mut x1, mut yh) = (0.0, 0.0, 1.0, 1.0);
    let mut nflg = false;
    let (mut a, mut b, mut y0, mut x, mut y);
    let mut rflg;
    let mut dithresh;
    let mut lgm;
    let mut step;

    if aa <= 1.0 || bb <= 1.0 {
        dithresh = 1.0e-6;
        rflg = false;
        a = aa;
        b = bb;
        y0 = yy0;
        x = a / (a + b);
        y = incbet(a, b, x);
        step = Step::Ihalve;
    } else {
        dithresh = 1.0e-4;
        let mut yp = -ndtri(yy0);
        if yy0 > 0.5 {
            rflg = true;
            a = bb;
            b = aa;
            y0 = 1.0 - yy0;
            yp = -yp;
        } else {
            rflg = false;
            a = aa;
            b = bb;
            y0 = yy0;
        }
        lgm = (yp * yp - 3.0) / 6.0;
        x = 2.0 / (1.0 / (2.0 * a - 1.0) + 1.0 / (2.0 * b - 1.0));
        let mut d = yp * sqrt(x + lgm) / x
            - (1.0 / (2.0 * b - 1.0) - 1.0 / (2.0 * a - 1.0)) * (lgm + 5.0 / 6.0 - 2.0 / (3.0 * x));
        d *= 2.0;
        if d < MIN_LOG {
            x = 0.0;
            y = 0.0;
            step = Step::Done;
        } else {
            x = a / (a + b * exp(d));
            y = incbet(a, b, x);
            let yp = (y - y0) / y0;
            step = if fabs(yp) < 0.2 { Step::Newt } else { Step::Ihalve };
        }
    }

    loop {
        match step {
            Step::Ihalve => {
                let mut dir = 0i32;
                let mut di = 0.5;
                step = Step::Newt; // the fall-through after the loop, unless set below
                let mut restart = false;
                for i in 0..100 {
                    if i != 0 {
                        x = x0 + di * (x1 - x0);
                        if x == 1.0 {
                            x = 1.0 - MACH_EP;
                        }
                        if x == 0.0 {
                            di = 0.5;
                            x = x0 + di * (x1 - x0);
                            if x == 0.0 {
                                step = Step::Done;
                                break;
                            }
                        }
                        y = incbet(a, b, x);
                        let yp = (x1 - x0) / (x1 + x0);
                        if fabs(yp) < dithresh {
                            step = Step::Newt;
                            restart = true; // jump straight to newt
                            break;
                        }
                        let yp = (y - y0) / y0;
                        if fabs(yp) < dithresh {
                            step = Step::Newt;
                            restart = true;
                            break;
                        }
                    }
                    if y < y0 {
                        x0 = x;
                        yl = y;
                        if dir < 0 {
                            dir = 0;
                            di = 0.5;
                        } else if dir > 3 {
                            di = 1.0 - (1.0 - di) * (1.0 - di);
                        } else if dir > 1 {
                            di = 0.5 * di + 0.5;
                        } else {
                            di = (y0 - y) / (yh - yl);
                        }
                        dir += 1;
                        if x0 > 0.75 {
                            if rflg {
                                rflg = false;
                                a = aa;
                                b = bb;
                                y0 = yy0;
                            } else {
                                rflg = true;
                                a = bb;
                                b = aa;
                                y0 = 1.0 - yy0;
                            }
                            x = 1.0 - x;
                            y = incbet(a, b, x);
                            x0 = 0.0;
                            yl = 0.0;
                            x1 = 1.0;
                            yh = 1.0;
                            step = Step::Ihalve;
                            restart = true;
                            break;
                        }
                    } else {
                        x1 = x;
                        if rflg && x1 < MACH_EP {
                            x = 0.0;
                            step = Step::Done;
                            restart = true;
                            break;
                        }
                        yh = y;
                        if dir > 0 {
                            dir = 0;
                            di = 0.5;
                        } else if dir < -3 {
                            di *= di;
                        } else if dir < -1 {
                            di *= 0.5;
                        } else {
                            di = (y - y0) / (yh - yl);
                        }
                        dir -= 1;
                    }
                }
                if restart || matches!(step, Step::Done) {
                    continue;
                }
                // The loop ran out: Cephes' checks before falling into newt.
                if x0 >= 1.0 {
                    x = 1.0 - MACH_EP;
                    step = Step::Done;
                } else if x <= 0.0 {
                    x = 0.0;
                    step = Step::Done;
                } else {
                    step = Step::Newt;
                }
            }
            Step::Newt => {
                if nflg {
                    step = Step::Done;
                    continue;
                }
                nflg = true;
                lgm = lgamma(a + b) - lgamma(a) - lgamma(b);
                step = Step::Ihalve; // "did not converge", unless set below
                for i in 0..8 {
                    if i != 0 {
                        y = incbet(a, b, x);
                    }
                    if y < yl {
                        x = x0;
                        y = yl;
                    } else if y > yh {
                        x = x1;
                        y = yh;
                    } else if y < y0 {
                        x0 = x;
                        yl = y;
                    } else {
                        x1 = x;
                        yh = y;
                    }
                    if x == 1.0 || x == 0.0 {
                        break;
                    }
                    let mut d = (a - 1.0) * log(x) + (b - 1.0) * log(1.0 - x) + lgm;
                    if d < MIN_LOG {
                        step = Step::Done;
                        break;
                    }
                    if d > MAX_LOG {
                        break;
                    }
                    d = exp(d);
                    d = (y - y0) / d;
                    let mut xt = x - d;
                    if xt <= x0 {
                        let yy = (x - x0) / (x1 - x0);
                        xt = x0 + 0.5 * yy * (x - x0);
                        if xt <= 0.0 {
                            break;
                        }
                    }
                    if xt >= x1 {
                        let yy = (x1 - x) / (x1 - x0);
                        xt = x1 - 0.5 * yy * (x1 - x);
                        if xt >= 1.0 {
                            break;
                        }
                    }
                    x = xt;
                    if fabs(d / x) < 128.0 * MACH_EP {
                        step = Step::Done;
                        break;
                    }
                }
                if matches!(step, Step::Ihalve) {
                    dithresh = 256.0 * MACH_EP;
                }
            }
            Step::Done => {
                if rflg {
                    x = if x <= MACH_EP { 1.0 - MACH_EP } else { 1.0 - x };
                }
                return x;
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// `(a, b, p, gonum's distuv.Beta{a, b}.Quantile(p))`, from gonum v0.17.0.
    /// Shaped like COP's use: a count out of thousands of sims, tiny tails.
    const GONUM: [(f64, f64, f64, f64); 12] = [
        (1.0, 1000.0, 5e-05, 5.000124879160159e-08),
        (3.0, 998.0, 0.99995, 0.014631528596748766),
        (20.0, 981.0, 5e-05, 0.007104634891721992),
        (21.0, 980.0, 0.99995, 0.04317666988336166),
        (150.0, 9851.0, 1e-06, 0.00990478169979119),
        (151.0, 9850.0, 0.999999, 0.021603925348318564),
        (0.5, 0.5, 0.3, 0.20610737385376338),
        (2.0, 3.0, 0.7, 0.5084047548725843),
        (5000.0, 5001.0, 0.5, 0.4999500016667547),
        (1.0, 1.0, 0.25, 0.25),
        (9999.0, 2.0, 0.001, 0.9990770386693395),
        (40.0, 12000.0, 1.25e-05, 0.0015543158474091564),
    ];

    #[test]
    fn quantiles_match_gonum() {
        for (a, b, p, want) in GONUM {
            let got = beta_quantile(a, b, p);
            let rel = ((got - want) / want).abs();
            // Same algorithm; only the elementary functions differ from Go's,
            // so agreement is to the last few bits, not necessarily bitwise.
            assert!(rel < 1e-12, "Beta({a}, {b}).Quantile({p}) = {got}, gonum {want}");
        }
    }
}
