# Chordata: belt tension from plucks, measured with the toolhead accelerometer.
#
# A plucked belt span rings at f = sqrt(T / mu) / (2 L). The belt pulls on the carriage at
# that tone and at twice it (the pull at twice it goes with the swing squared, so it dies
# away faster), and the accelerometer on the toolhead picks both up.
#
# How a recording is read:
#  1. The recording is checked: the sample rate comes from the timestamps (lost messages
#     are gaps, and nothing is read across one), and samples at the sensor's limit are
#     marked.
#  2. The background (recorded with hands off) gives the noise level at each frequency and
#     the steady lines in it (fans, motors). Every threshold comes from that noise level and
#     a chosen false-alarm rate.
#  3. Events (plucks, taps, knocks) are where the recording stops being predictable: in
#     short frames, each frequency is compared with what the two frames before it predict
#     (same level, phase moving on at the same rate). A fan, or a pluck still ringing, is
#     predicted; a new pluck is not (complex-domain onset detection).
#  4. Each event is split into the decaying tones it holds: in a band around each line that
#     stands above the noise, a few decaying tones shared by the three axes are fitted
#     (variable projection, adding tones while a line is left above the noise). Each tone
#     gets a frequency with its uncertainty, a ring time, a level, and how much of it was
#     already there before the event (fitted to the stretch before it).
#  5. The tones are labelled: a tone that starts at the event and has a partner at twice it
#     that dies faster, or rings longer than a knock, is a belt; a tone that was there before
#     at the same level is steady (a fan); a tone at a mode the taps learned, with no partner,
#     is the toolhead.
#  6. The belt is read from its own tone, never from half of twice it (a belt is a stiff
#     string: its overtones sit a little sharp).
#  7. A pluck is left out when the reading can't be trusted, and the series is judged as
#     before (enough plucks, close together, not the other belt, not another stretch).
#
# Copyright (C) 2026  Chordata contributors
# This file may be distributed under the terms of the GNU GPLv3 license.
import math
import threading

import numpy as np

SEARCH = (25.0, 500.0)    # Hz, where lines are looked at (twice the tone included)
F_BAND = (0.55, 1.5)      # x target: where the tone itself is looked for
F_MAX = 200.0             # Hz: no belt tone above this unless the target is that high
SKIP = 0.03               # s after the onset: the attack (a click on every line) is left out
WINDOW = 0.6              # s of ringing used per pluck (less if the next pluck comes sooner)
DEAD = 0.45               # s: a second attack sooner than this is part of the same event
MERGE = 0.2               # s: a second attack this soon rings as one with the first (not judged)

# Judging a pluck
# (fitted energy against the noise's in the window, a matched filter: the old engine's 12 and
# 6 dB on a Hann periodogram, plus the 4 dB a Hann window loses on a tone that rings 0.3 s,
# so long-ringing plucks are judged as before and short-ringing ones no longer lose up to 17 dB)
STRONG_DB = 16.0          # dB: the stronger of tone / twice the tone above the noise (like 12 dB SINAD)
WEAK_DB = 10.0            # the weaker one: there at all
ATTACK_DB = 6.0           # dB: a tone that starts at an event: this much more new than carried on
KNOCK_TAU = 0.2           # s: with no taps learned, a tone with no partner dying faster than this (1/e of its level) is a knock
CLICK_TAU = 0.05          # s: with taps learned, only this fast (a click): real belts ring 0.08-0.25 s, toolheads 0.05-0.16 s
TAP_GUARD = 2.0           # Hz around the toolhead's own ringing (learned from a tap), at least
GUARD_MAX = 8.0           # Hz: a broad, damped toolhead line is guarded this wide at most
OTHER_GUARD = 1.0         # Hz: a pluck this close to the other belt is that belt ringing
SPREAD = 1.0              # Hz: accepted plucks must all be this close to their median
SPAN = 0.05               # share: plucks this far from the rest rang on another stretch of belt
MIN_GOOD = 3              # fewer accepted plucks than this: no reading
PAIR_RATIO = (1.2, 4.0)   # how many times faster a belt's 2F decays than its tone (2 in theory; 1.3-3.2 measured)
SMEAR_HZ = 8.0            # Hz: a second pluck 0.15-0.3 s after the first smears the fit over about this (1/dt and twice)
SMEAR_TAU = 0.1           # s: smear tones ring like the belt; shorter ones are the pluck's own transients (36 real plucks
                          # had flanking tones on both sides, 3 of them ringing this long on both)
SMEAR_SHARE = 0.03        # of the tone's strength (-15 dB): weaker neighbours are the model's own leftovers
HALF_SHARE = 0.02         # of the tone: how far twice a tone at half it may sit (2F sharp by up to 1 %, a weak F's own +-)
OCT_NEAR = 0.15           # the belt's last reading settles an octave doubt when it is within this (log) of one
OCT_TIE = 0.7             # scores this close: the octave nearer the belt's last reading wins
SIGMA_MAX = 0.2           # Hz: a pluck read less surely than this is left out. On the twin sensors (the same plucks read by an ADXL345 and a Beacon) pairs with +- 0.1-0.2 Hz agreed about as well as those under 0.1 (90 % within 0.31 against 0.25 Hz; 5 pairs against 14); real Nebula plucks fit at +- 0.07-0.19, and 0.1 refused a quarter of them
# A steady line closer to the tone than the window resolves (1 / its length) is fitted as one
# with it. Holding a sixteenth of the tone's energy it can pull the reading by up to ~0.1 Hz
# (a single-tone reading sits near the energy-weighted mean of the two): more is "hidden".
HIDE_DB = 12.0
HUM_HZ = 1.0 / WINDOW + 1.0  # a background hum this close to a belt's note (Hz): turn it off first
BESIDE_DB = 3.0           # dB: a tone closer than the window resolves and within this of the pluck's: can't tell which belt was plucked
PAIR_HZ = 1.0             # Hz: twice the tone is looked for this far either side of 2F, at least
PAIR_SHARE = 0.01         # or this share of 2F (a stiff belt's overtone sits sharp, ~0.5-1 Hz at 220 Hz)

# The tone model
MODEL_HALF = 10.0         # Hz either side of a band's centre that are modelled
MODEL_TAPER = 4.0         # Hz of soft edge outside that
MODEL_PAD = 0.35          # s of real data either side, filtered with it (the filter's tails)
EDGE = 0.02               # s of soft edge where that data is cut short (no jump for the filter to ring at)
MODEL_KMAX = 6            # tones in one band at most
MODEL_PFA = 1e-2          # chance that pure noise adds a tone to a band
EVENT_PFA = 1e-3          # chance that pure noise makes a new tone, i.e. an event
BANDS_MAX = 8             # bands fitted per event at most (the strongest lines first)
PRE = 0.6                 # s before an event that tell what was there already
CARRY = 0.3               # s just before it that the carried-on part is extrapolated from
DYN = 1e-3                # the model's dynamic range (energy): -30 dB of the event's loudest tone
RENEW = 0.25              # share of a decaying tone's level that a new pluck of it adds, at least
JUNK_TAU = 0.03           # s: a fitted "tone" dying this fast is the attack smeared by the band filter (as long as SKIP)
LEAD = 0.06               # s before the onset where "before the event" ends (a soft attack rises for up to this long before its half-way point)
BEAT = 0.3                # s: a level that comes back within this (a beat) is not new; beats of 3.3 Hz and up
RISE = 0.8                # share of that recent top the level has to pass (a beat's next peak has decayed below it)
CLOSE = 0.25              # closest two tones of one fit, in 1/T (closer is one tone: a split that close is ill-posed)
CANCEL = 4.0              # tones together at most this many times the window's energy (else they cancel in pairs)

# Finding events
FRAME = 0.064             # s: short frames for the onset detector
HOP = 0.01                # s between them
ONSET_K = 8.0             # robust standard deviations of the background's deviation: an onset
# (and an attack's frames reach twice that: the ringing after a loud attack hovers near it)

TAP_NEED = 3              # good taps for a fingerprint
TAP_FAILS = 9             # bad taps: stop and tell the user to look for what shakes the printer


# ---------------- small helpers ----------------
def _db(x):
    return 10.0 * math.log10(max(x, 1e-30))


def _pow2(n):
    return 1 << int(math.ceil(math.log2(max(2, n))))


def _running_median(x, n):
    h = n // 2
    pad = np.pad(x, h, mode="edge")
    try:
        view = np.lib.stride_tricks.sliding_window_view(pad, n)
    except AttributeError:            # numpy < 1.20
        return np.array([np.median(pad[i:i + n]) for i in range(len(x))])
    return np.median(view, axis=1)


def _analytic(a):
    """The analytic signal of a (its positive frequencies only, at the same level): a real
    tone becomes one turning phasor, which a short frame's phase predicts exactly whatever
    the window (a real one has a mirror at minus its frequency that leaks in and turns the
    other way: beside a loud ringing line that leak looks like something new)."""
    a = np.asarray(a, float)
    n = len(a)
    X = np.fft.fft(a - a.mean())
    h = np.zeros(n)
    h[0] = 1.0
    h[1:(n + 1) // 2] = 2.0
    if n % 2 == 0:
        h[n // 2] = 1.0
    return np.fft.ifft(X * h)


def _frames(a, n, hop):
    """Frames of n samples, hop apart, as a strided view (no copy)."""
    count = (len(a) - n) // hop + 1
    if count <= 0:
        return np.zeros((0, n))
    return np.lib.stride_tricks.as_strided(a, (count, n), (a.strides[0] * hop, a.strides[0]))


def _gamma_sf(k, x):
    """P(X > x) for X the sum of k unit-mean exponentials (k axes' periodograms of noise)."""
    if x <= 0:
        return 1.0
    term, s = 1.0, 1.0
    for i in range(1, k):
        term *= x / i
        s += term
    return math.exp(-x) * s


def _gamma_isf(k, p):
    lo, hi = 0.0, k + 60.0 * math.sqrt(k) + 60.0
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if _gamma_sf(k, mid) > p:
            lo = mid
        else:
            hi = mid
    return hi


def _f_range(lo, hi, target):
    """Where the tone itself is looked for."""
    if target:
        return max(lo, F_BAND[0] * target), min(hi, F_BAND[1] * target, max(F_MAX, 1.2 * target))
    return lo, min(hi, F_MAX)


def _pair_tol(f2):
    return max(PAIR_HZ, PAIR_SHARE * f2)


# ---------------- 1. the recording ----------------
class _Rec(object):
    """A recording: the three axes, the sample rate from the timestamps, where samples were
    lost (gaps) and where the sensor was at its limit (clipped)."""

    def __init__(self, t, axes):
        self.t = t
        self.axes = axes
        self.n = len(t)
        dt = np.diff(t)
        nominal = float(np.median(dt))
        ok = dt <= 1.5 * nominal
        # the rate from the steps between samples, a lost message left out (the ADXL345's own
        # clock is off by up to +-10 %, and its rate changes from one recording to the next)
        self.fs = float(np.count_nonzero(ok) / max(float(np.sum(dt[ok])), 1e-12))
        self.gaps = np.flatnonzero(~ok) + 1          # the first sample after each gap
        self.clip = np.zeros(self.n, bool)
        for a in axes:
            for extreme in (a.max(), a.min()):
                at = a == extreme
                # a sensor at its limit sends the same value over and over: three in a row at
                # the recording's extreme, far above its noise
                run = at[:-2] & at[1:-1] & at[2:]
                noise = 1.4826 * float(np.median(np.abs(np.diff(a)))) / math.sqrt(2.0) + 1e-12
                if run.any() and abs(extreme - float(np.median(a))) > 50.0 * noise:
                    idx = np.flatnonzero(run)
                    self.clip[idx] = self.clip[idx + 1] = self.clip[idx + 2] = True

    def gap_after(self, i):
        """The first gap after sample i (or the end)."""
        k = int(np.searchsorted(self.gaps, i, side="right"))
        return int(self.gaps[k]) if k < len(self.gaps) else self.n

    def gap_before(self, i):
        """The start of the stretch without gaps that holds sample i."""
        k = int(np.searchsorted(self.gaps, i, side="right"))
        return int(self.gaps[k - 1]) if k > 0 else 0


def _setup(t, ax, ay, az, quiet):
    t = np.asarray(t, float)
    axes = [np.asarray(a, float) for a in (ax, ay, az)]
    if t.ndim != 1 or any(a.ndim != 1 or len(a) != len(t) for a in axes):
        raise ValueError("recording must contain equally sized one-dimensional time and acceleration arrays")
    if not np.isfinite(t).all() or any(not np.isfinite(a).all() for a in axes):
        raise ValueError("recording contains non-finite samples")
    if len(t) > 1 and not np.all(np.diff(t) > 0):
        raise ValueError("recording timestamps must increase strictly")
    if len(t) < 100:
        return None
    rec = _Rec(t, axes)
    if rec.fs <= 2 * SEARCH[0] / 0.45:
        raise ValueError("accelerometer sample rate is too low for belt analysis")
    q = None
    if quiet is not None:
        if len(quiet) != 3:
            raise ValueError("quiet recording must contain three acceleration arrays")
        qa = [np.asarray(a, float) for a in quiet]
        if any(a.ndim != 1 or len(a) != len(qa[0]) or not np.isfinite(a).all() for a in qa):
            raise ValueError("quiet recording contains invalid acceleration samples")
        if len(qa[0]) > int(0.5 * rec.fs):
            q = qa
    return rec, q


# ---------------- 2. the background ----------------
class _Floor(object):
    """The noise level at each frequency: per axis, as what a Hann periodogram of one axis
    expects per unit of sum(w^2) (the white-noise variance, for white noise). Lines (fans,
    motors) are left out of it by a running median over +-10 Hz."""

    def __init__(self, ref, fs, pct=50):
        total = len(ref[0])
        n = min(int(0.5 * fs), total)
        self.ok = n >= int(0.2 * fs)
        if not self.ok:
            return
        nfft = _pow2(n)
        w = np.hanning(n)
        hop = max(1, n // 4)
        p = 0
        for a in ref:
            fr = _frames(np.ascontiguousarray(a, float), n, hop)
            fr = (fr - fr.mean(axis=1, keepdims=True)) * w
            p = p + np.abs(np.fft.rfft(fr, nfft, axis=1)) ** 2
        bg = np.percentile(p, pct, axis=0)
        self.f = np.fft.rfftfreq(nfft, 1.0 / fs)
        df = self.f[1] - self.f[0]
        # the pct-th percentile of three axes' noise periodograms is that quantile of a
        # Gamma(3) times one axis's mean
        q = _gamma_isf(3, 1.0 - pct / 100.0)
        self.v = np.maximum(_running_median(bg, int(20.0 / df) | 1) / (q * float(np.sum(w * w))), 1e-30)
        self.key = (n, pct, round(float(np.median(self.v)), 9))

    def __call__(self, freqs):
        return np.interp(freqs, self.f, self.v)


def _lines(ref, fs, floor):
    """Steady lines in a recording without plucks: (Hz, level), level as a tone's summed
    squared amplitude per axis (the units of a fitted tone's level). Long windows keep them
    narrow; a line is a peak that the noise would reach only with a 1 % chance anywhere in the
    band."""
    total = len(ref[0])
    n = min(int(1.0 * fs), int(total / 1.5))
    if n < int(0.25 * fs) or not floor.ok:
        return []
    nfft = _pow2(4 * n)
    w = np.hanning(n)
    hop = max(1, n // 2)
    p = 0
    for a in ref:
        fr = _frames(np.ascontiguousarray(a, float), n, hop)
        fr = (fr - fr.mean(axis=1, keepdims=True)) * w
        p = p + np.abs(np.fft.rfft(fr, nfft, axis=1)) ** 2
    k_win = p.shape[0]
    p = p.mean(axis=0)
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    ratio = p / (3.0 * floor(f) * float(np.sum(w * w)))
    sel = (f >= SEARCH[0]) & (f <= min(SEARCH[1], 0.45 * fs))
    cells = max(1.0, (SEARCH[1] - SEARCH[0]) * n / fs)
    thr = _gamma_isf(3 * k_win, 1e-2 / cells) / (3.0 * k_win)
    res = fs / n
    idx = np.flatnonzero(sel & (ratio > thr))
    idx = [k for k in idx if 0 < k < len(p) - 1 and p[k] >= p[k - 1] and p[k] >= p[k + 1]]
    kept = []

    def leak(j, k):
        # what a stronger line j puts at k through the Hann window: its main lobe (and a fan's
        # wandering speed) out to 2 bins, then sidelobes from -31.5 dB at 2.4 bins falling
        # 18 dB an octave; a line has to stand 6 dB above that to be one of its own
        d = abs(f[k] - f[j]) / res
        if d <= 2.0:
            return float("inf")
        return p[j] * 10 ** ((-31.5 + 6.0 - 18.0 * math.log2(max(d, 2.4) / 2.4)) / 10.0)
    for k in sorted(idx, key=lambda k: -p[k]):
        if any(p[k] <= leak(j, k) for j in kept):
            continue                    # the skirt or a sidelobe of a stronger line
        kept.append(k)
    return sorted((round(float(f[k]), 2), float(p[k] / float(np.sum(w)) ** 2)) for k in kept)


def steady_lines(axes, fs):
    """The steady lines (Hz) in a recording without plucks."""
    axes = [np.asarray(a, float) for a in axes]
    floor = _Floor(axes, fs)
    return [x for x, _ in _lines(axes, fs, floor)] if floor.ok else []


def disturbing(quiet, fs, notes):
    """Steady lines in the background within HUM_HZ of a belt's note (notes: the target, the
    belt's last readings), loudest first: (Hz, the note it is near). A fan that close can hide
    the belt (closer than a pluck's window resolves); removing it beats measuring around it.
    Not twice the note, and not further off: the fit tells a steady line from a decaying
    tone there (the A/B's fan scenario, fans 2-8 Hz from F or 2F: no reading wrong), and on
    the Nebula a 211 Hz motor/controller-fan hum, 9 Hz from twice a 110 Hz target, stopped
    the tap step for nothing. A hum that does sit on a pluck is still refused per pluck."""
    q = [np.asarray(a, float) for a in quiet]
    floor = _Floor(q, fs)
    if not floor.ok:
        return []
    out = []
    for x, lv in sorted(_lines(q, fs, floor), key=lambda c: -c[1]):
        for y in notes or ():
            if y and abs(x - y) <= HUM_HZ:
                out.append((x, y))
                break
    return out


# ---------------- 3. events ----------------
def _deviation(axes, fs, floor, lo, hi, gaps=()):
    """Rectified complex-domain deviation per frame (Bello 2005; Dixon 2006): each frequency
    in a short frame, divided by the noise there, against what the two frames before predict
    (the same level, the phase moving on at the same rate), summed where the level rose.
    Returns (frame start samples, deviation, frame length)."""
    n = max(16, int(FRAME * fs))
    hop = max(1, int(HOP * fs))
    nfft = _pow2(n)
    w = np.hanning(n)
    f = np.fft.fftfreq(nfft, 1.0 / fs)
    sel = (f >= lo) & (f <= hi)
    scale = 0.5 / np.sqrt(floor(f[sel]) * float(np.sum(w * w)))
    d = None
    for a in axes:
        fr = _frames(_analytic(a), n, hop)
        if len(fr) < 3:
            return np.zeros(0, int), np.zeros(0), n
        X = np.fft.fft((fr - fr.mean(axis=1, keepdims=True)) * w, nfft, axis=1)[:, sel] * scale
        mag = np.abs(X)
        ph = np.angle(X)
        pred = mag[1:-1] * np.exp(1j * (2.0 * ph[1:-1] - ph[:-2]))
        # each frequency's deviation against what it would be with nothing new: noise in a
        # bin's own (unit) level, plus noise in a strong line's phase, which the prediction
        # carries on (2 phi1 - phi2: about four times the noise), plus a line's own small
        # changes (a fan's wandering speed, a decay): a thousandth of its level
        e = np.abs(pred) ** 2
        # counted where the level rose, and above what that frequency had over the last BEAT s
        # (two tones in one bin beat: the level dips and comes back, never above the last peak
        # of the beat, which has decayed since)
        k = max(1, int(BEAT / HOP))
        back = _runmax(mag, k)
        rose = (mag[2:] > mag[1:-1]) & (mag[2:] > RISE * back[1:-1])
        dev = np.abs(X[2:] - pred) ** 2 * rose / (2.0 + 3.0 * e / (e + 1.0) + 1e-3 * e)
        dd = np.concatenate([np.zeros(2), dev.sum(axis=1)])
        d = dd if d is None else d + dd
    starts = np.arange(len(d)) * hop
    for g in gaps:
        # a frame across a gap, and the two after it (they predict from it), say nothing
        bad = (starts < g) & (starts + n + 2 * hop > g)
        d[bad] = 0.0
    return starts, d, n


def _runmax(x, k):
    """The running maximum along axis 0 over the last k rows (this one included)."""
    out = x.copy()
    step = 1
    while step < k:                 # doubling: max over [i - 2*step + 1, i]
        s = min(step, k - step)
        out[s:] = np.maximum(out[s:], out[:-s])
        step += s
    return out


def _threshold(d):
    """Onset threshold from a deviation series without (or mostly without) events: its
    median plus ONSET_K robust standard deviations."""
    d = d[d > 0]
    if not len(d):
        return float("inf")
    med = float(np.median(d))
    mad = 1.4826 * float(np.median(np.abs(d - med)))
    return med + ONSET_K * max(mad, 0.05 * med)


def _attack(rec, floor, s, e):
    """Where the attack is between samples s and e: the same deviation as the onset detector,
    sample by sample, in frames whose window is flat at its end (an attack counts in full as
    soon as it comes in; a steady or ringing line is still predicted exactly, whatever the
    window): the first sample halfway from before the attack to its top."""
    fs = rec.fs
    n = max(16, int(FRAME * fs))
    nfft = _pow2(n)
    w = np.ones(n)
    w[:n // 2] = 0.5 - 0.5 * np.cos(np.pi * np.arange(n // 2) / (n // 2))
    f = np.fft.fftfreq(nfft, 1.0 / fs)
    sel = (f >= SEARCH[0]) & (f <= min(SEARCH[1], 0.45 * fs))
    scale = 0.5 / np.sqrt(floor(f[sel]) * float(np.sum(w * w)))
    hop = max(1, int(0.001 * fs))
    b0 = s - n - int(0.03 * fs) - 2 * hop           # frames ending from 30 ms before s
    if b0 < 0 or e + 1 > rec.n:
        return s
    pad = min(b0, rec.n - e - 1, n)                 # (the analytic signal's ends are off)
    d = 0
    for a in rec.axes:
        z = _analytic(a[b0 - pad:e + 1 + pad])[pad:len(a[b0 - pad:e + 1 + pad]) - pad]
        fr = _frames(z, n, hop)
        X = np.fft.fft((fr - fr.mean(axis=1, keepdims=True)) * w, nfft, axis=1)[:, sel] * scale
        mag, ph = np.abs(X), np.angle(X)
        pred = mag[1:-1] * np.exp(1j * (2.0 * ph[1:-1] - ph[:-2]))
        en = np.abs(pred) ** 2
        d = d + (np.abs(X[2:] - pred) ** 2 * (mag[2:] > mag[1:-1]) / (2.0 + 3.0 * en / (en + 1.0) + 1e-3 * en)).sum(axis=1)
    ends = b0 + n + (np.arange(len(d)) + 2) * hop
    pre = ends < s
    if not pre.any() or d.max() <= 0:
        return s
    before = float(np.median(d[pre]))
    top = float(d[~pre].max()) if (~pre).any() else 0.0
    if top <= before:
        return s
    hit = np.flatnonzero(~pre & (d >= before + 0.5 * (top - before)))
    return int(ends[hit[0]]) - hop // 2 if len(hit) else s


def _candidates(rec, floor, q, lines):
    """Sample indices where something new starts (plucks, taps, knocks, and a few that turn
    out to be nothing new: the tone model has the last word)."""
    fs = rec.fs
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    starts, d, n = _deviation(rec.axes, fs, floor, lo, hi, rec.gaps)
    if not len(d):
        return []
    thr = None
    if q is not None:
        _, dq, _ = _deviation(q, fs, floor, lo, hi)
        if len(dq) >= 20:
            thr = _threshold(dq[2:])
    if thr is None:
        thr = _threshold(d[2:])
    # each attack is a peak of the deviation, clear of the frames around it (the ringing
    # after a loud attack hovers near the threshold, so a peak counts at twice it)
    hop = max(1, int(HOP * fs))
    sep = max(1, int(MERGE * fs / hop))         # (an attack this close rings as one with it)
    out = []
    for k in np.flatnonzero(d >= 2.0 * thr):
        # the top of its own peak, and not inside the rise of a louder attack just before it
        # (a soft attack still counts when two belts beating make taller peaks after it:
        # those start no tone, and the tone model drops them)
        if d[k] < d[max(0, k - sep):k + 3].max():
            continue
        # the attack came in during this frame
        s = int(starts[k])
        out.append(_attack(rec, floor, s, min(rec.n - 1, s + n + hop)))
    return sorted(set(out))


def find_plucks(axes, fs, lo, hi, mask=None, base=None, env=None):
    """Onset indices of the events in a recording (for tools and tests)."""
    axes = [np.asarray(a, float) for a in axes]
    t = np.arange(len(axes[0])) / fs
    rec = _Rec(t, axes)
    rec.fs = fs
    floor = _Floor(axes, fs, pct=20)
    if not floor.ok:
        return []
    return [on for on, _ in _events(rec, floor, None, _Ctx())]


# ---------------- 4. the tone model ----------------
class _Band(object):
    """The three axes around `centre` Hz as a complex baseband, from samples lo..hi (with
    MODEL_PAD of real data either side, so the filter's tails don't reach the window). The
    phase is counted from the recording's first sample: a tone has the same phase before
    and after an event."""

    def __init__(self, rec, centre, lo, hi, lo_lim=0, hi_lim=None):
        fs = rec.fs
        p = int(MODEL_PAD * fs)
        # never across a gap, nor into the event before or after (its attack, far louder than
        # what is fitted here, would leak in through the filter's tails)
        self.b0 = max(rec.gap_before(lo), lo - p, lo_lim)
        self.b1 = min(rec.gap_after(hi - 1), hi + p, rec.n if hi_lim is None else hi_lim)
        seg = np.array([a[self.b0:self.b1] for a in rec.axes], float)
        seg -= seg.mean(axis=1, keepdims=True)
        k = min(int(EDGE * fs), seg.shape[1] // 4)
        if k > 1:
            ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(k) / k)
            seg[:, :k] *= ramp
            seg[:, -k:] *= ramp[::-1]
        n = seg.shape[1]
        f = np.fft.fftfreq(n, 1.0 / fs)
        g = np.clip((MODEL_HALF + MODEL_TAPER - np.abs(f - centre)) / MODEL_TAPER, 0.0, 1.0)
        g = 0.5 - 0.5 * np.cos(np.pi * g)
        z = np.fft.ifft(np.fft.fft(seg, axis=1) * g, axis=1)
        self.z = z * np.exp(-2j * math.pi * centre * np.arange(self.b0, self.b1) / fs)
        self.centre = centre
        self.dec = max(1, int(fs / (4.0 * (MODEL_HALF + MODEL_TAPER))))
        self.fz = fs / self.dec
        self.bw = float(np.sum(g * g) * fs / n)       # noise bandwidth, Hz
        self.over = self.fz / self.bw                 # decimated samples per independent one

    def get(self, a0, a1):
        a0, a1 = max(a0, self.b0), min(a1, self.b1)
        return self.z[:, a0 - self.b0:max(a0 - self.b0, a1 - self.b0):self.dec]


def _basis(p, m, fz):
    t = np.arange(m) / fz
    return np.exp(np.outer(t, -p[1::2] + 2j * math.pi * p[0::2]))


def _project(Z, E):
    A = np.linalg.lstsq(E, Z.T, rcond=None)[0]
    return A, Z.T - E @ A


def _rss(R):
    return float(np.sum(R.real ** 2 + R.imag ** 2))


def _jac(Z, p, A, R, fz):
    """The exact Jacobian of the leftover R = Z.T - E A (amplitudes solved for p) by p
    (Golub and Pereyra): dR = -(P dE A + pinv(E)^H dE^H R), with P the projector off the tones."""
    m = Z.shape[1]
    t = np.arange(m) / fz
    E = _basis(p, m, fz)
    Q, U = np.linalg.qr(E)
    try:
        Uh = np.linalg.inv(U).conj().T
    except np.linalg.LinAlgError:
        Uh = np.linalg.pinv(U).conj().T
    r0 = np.concatenate([R.real.ravel(), R.imag.ravel()])
    J = np.empty((len(r0), len(p)))
    for k in range(len(p)):
        j = k // 2
        de = (2j * math.pi if k % 2 == 0 else -1.0) * t * E[:, j]
        v = np.outer(de, A[j])
        v -= Q @ (Q.conj().T @ v)
        dR = -(v + np.outer(Q @ Uh[:, j], de.conj() @ R))
        J[:, k] = np.concatenate([dR.real.ravel(), dR.imag.ravel()])
    return J, r0


def _fit(Z, fz, comps, span, amax, iters=40):
    """Levenberg-Marquardt on (Hz from the centre, decay rate 1/s) of each tone, the
    amplitudes solved exactly (variable projection). Returns params (K, 2), amplitudes
    (K, 3), residual (m, 3) and the unscaled covariance of the params."""
    m = Z.shape[1]
    k = len(comps)
    lo, hi = np.tile([-span, -0.3], k), np.tile([span, amax], k)
    p = np.clip(np.array(comps, float).ravel(), lo, hi)
    A, R = _project(Z, _basis(p, m, fz))
    c = _rss(R)
    lam = 1e-2
    for _ in range(iters):
        J, r0 = _jac(Z, p, A, R, fz)
        H, g = J.T @ J, J.T @ r0
        D = np.diag(np.diag(H)) + 1e-12 * np.eye(len(p))
        # a parameter on its bound that the descent pushes further out stays there: left in the
        # step, the clip would cut its share off every time and the rest would crawl
        free = ~(((p <= lo) & (g > 0)) | ((p >= hi) & (g < 0)))
        if not free.any():
            break
        Hf, Df, gf = H[np.ix_(free, free)], D[np.ix_(free, free)], g[free]
        moved = False
        for _ in range(12):
            try:
                q = p.copy()
                q[free] -= np.linalg.solve(Hf + lam * Df, gf)
                q = np.clip(q, lo, hi)
            except np.linalg.LinAlgError:
                lam *= 10.0
                continue
            Aq, Rq = _project(Z, _basis(q, m, fz))
            cq = _rss(Rq)
            if cq < c:
                # done when the step is tiny, or gains next to nothing: c is about 6m noise
                # variances, so 1e-7 c is 1e-4 of one in chi-square, a step of 1 % of sigma
                small = np.all(np.abs(q - p) < np.tile([1e-5, 1e-4], k)) or c - cq < 1e-7 * c
                p, A, R, c = q, Aq, Rq, cq
                lam = max(lam / 4.0, 1e-9)
                moved = not small
                break
            lam *= 5.0
        if not moved:
            break
    J, _ = _jac(Z, p, A, R, fz)
    try:
        cov = np.linalg.inv(J.T @ J)
    except np.linalg.LinAlgError:
        cov = np.full((len(p), len(p)), np.inf)
    return p.reshape(-1, 2), A, R, cov


def _sd(var, s2):
    """A parameter's standard deviation from its unscaled variance; a negative or broken one
    (a near-singular fit) is not known at all, never zero."""
    v = float(var)
    return math.sqrt(v * s2 / 2.0) if math.isfinite(v) and v >= 0.0 else float("inf")


def _illposed(Z, fit, fz):
    """True when a fit has more tones than the window can tell apart: two of them closer
    than CLOSE/T, a near-singular basis, or tones far louder than the data (huge amplitudes
    cancelling each other)."""
    P, A = fit[0], fit[1]
    m = Z.shape[1]
    if len(P) > 1:
        d = np.abs(P[:, None, 0] - P[None, :, 0]) + np.eye(len(P)) * 1e9
        if d.min() < CLOSE * fz / m:
            return True
    E = _basis(P.ravel(), m, fz)
    if np.linalg.cond(E) > 1e5:
        return True
    parts = float(np.sum(np.sum(np.abs(A) ** 2, axis=1) * np.sum(np.abs(E) ** 2, axis=0)))
    return parts > CANCEL * _rss(Z)


def _relax(Z, band, seeds, sigma2, thr):
    """The window Z of `band` as the seed tones (Hz from the centre, decay rate) plus as many
    more as the leftover shows lines above thr (a three-axis periodogram of the leftover,
    against the noise). Returns (params, amplitudes, leftover, covariance) or None."""
    m = Z.shape[1]
    if m < 8:
        return None
    fz = band.fz
    span = MODEL_HALF + 0.5 * MODEL_TAPER
    amax = 2.0 * math.pi * MODEL_HALF
    nfft = _pow2(16 * m)
    w = np.hanning(m + 2)[1:-1]
    w2 = float(np.sum(w * w))
    fr = np.fft.fftfreq(nfft, 1.0 / fz)
    inner = np.abs(fr) <= MODEL_HALF
    comps = [(float(d), float(a)) for d, a in seeds if abs(d) <= span]
    fit = None
    R = Z.T
    while True:
        if comps:
            trial = _fit(Z, fz, comps, span, amax)
            # tones the window can't tell apart (nearly the same frequency and decay) make the
            # amplitudes cancel in huge pairs: then the model has one tone too many
            if _illposed(Z, trial, fz):
                if fit is not None:
                    break
                trial = _fit(Z, fz, comps[:1], span, amax)
            fit = trial
            R = fit[2]
        if len(comps) >= MODEL_KMAX:
            break
        P = np.sum(np.abs(np.fft.fft(R * w[:, None], nfft, axis=0)) ** 2, axis=1) / (w2 * sigma2 * band.over)
        k = int(np.argmax(np.where(inner, P, 0.0)))
        if P[k] < thr or (fit is not None and np.min(np.abs(fit[0][:, 0] - fr[k])) < CLOSE / (m / fz)):
            break                   # nothing left above the noise, or only right on a tone already fitted
        comps = [tuple(c) for c in fit[0]] if fit else []
        comps.append((float(fr[k]), 5.0))
    return fit


def _carry(band, tones, a0, p0, p1):
    """What each tone already was before the event: fitted (its own frequency and decay) to
    the stretch p0..p1 and carried on to the window's start a0, as complex amplitudes per
    axis (K, 3). tones: (Hz from the centre, decay rate) of every tone of the band's fit (a
    tone left out would leak into the others). None where the stretch is too short."""
    Zp = band.get(p0, p1)
    m = Zp.shape[1]
    if m < 6 or not len(tones):
        return None
    p = np.array(tones, float).ravel()
    A = _project(Zp, _basis(p, m, band.fz))[0]
    dt = (a0 - p0) / (band.fz * band.dec)
    g = np.exp((-p[1::2] + 2j * math.pi * p[0::2]) * dt)
    return A * g[:, None]


def _halves(band, tones, p0, p1):
    """Each tone's level (no decay) in the first and the second half of p0..p1."""
    mid = (p0 + p1) // 2
    out = []
    for b0, b1 in ((p0, mid), (mid, p1)):
        Zp = band.get(b0, b1)
        m = Zp.shape[1]
        if m < 4:
            return None
        p = np.array([[d, 0.0] for d, _ in tones]).ravel()
        A = _project(Zp, _basis(p, m, band.fz))[0]
        out.append(np.sum(np.abs(A) ** 2, axis=1))
    return out


def _free_line(band, d0, p0, p1):
    """The frequency (Hz) of the steady line near d0 Hz from the band's centre in p0..p1."""
    Zp = band.get(p0, p1)
    m = Zp.shape[1]
    if m < 6:
        return band.centre + d0
    best = None
    for d in np.arange(d0 - 1.0, d0 + 1.0001, 0.02):
        r = _rss(_project(Zp, _basis(np.array([d, 0.0]), m, band.fz))[1])
        if best is None or r < best[0]:
            best = (r, d)
    return band.centre + float(best[1])


class _Ctx(object):
    """What an event's tones are read against: the steady lines of the background, the
    toolhead's modes (Hz, guard, tau), where the tone is looked for, and bands to fit anyway."""

    def __init__(self, lines=(), modes=(), frange=None, force=()):
        self.lines = tuple(lines)
        self.modes = tuple(modes)
        self.frange = frange
        self.force = tuple(force)

    def key(self):
        return (tuple((round(x, 2), round(lv, 6)) for x, lv in self.lines),
                tuple((round(m, 2), round(g, 2), round(t, 3)) for m, g, t in self.modes),
                None if self.frange is None else tuple(round(x, 2) for x in self.frange),
                tuple(round(x, 2) for x in self.force))


_EVENTS = {}
_EVENTS_LOCK = threading.Lock()     # (a live analysis may still run beside the final one)
EVENTS_MAX = 96     # events cached at most, the least recently used go first (a 60 s listen has
                    # about 45; one is about 60 kB)


def clear_cache():
    """Forget the cached event fits (a new session, or Chordata closed)."""
    with _EVENTS_LOCK:
        _EVENTS.clear()


def _event(rec, floor, on, nxt, prev_on, ctx):
    """The tones of the event at sample `on`: fitted to its window (on + SKIP, at most WINDOW
    long, ending before the next event at nxt), each with how much of it was carried on from
    before (from prev_on, the event before, at the earliest). Cached: the live count analyses
    the same events again every second."""
    fs = rec.fs
    a0 = on + int(SKIP * fs)
    lim = rec.n if nxt is None else nxt - int(0.005 * fs)
    a1 = min(a0 + int(WINDOW * fs), lim - int((EDGE + 0.005) * fs) if nxt is not None else lim,
             rec.gap_after(on), rec.n)
    if a1 - a0 < int(0.12 * fs):
        return None
    p1 = on - int(LEAD * fs)
    p0 = max(p1 - int(PRE * fs), rec.gap_before(max(0, on)), 0,
             (prev_on + int((SKIP + EDGE + 0.01) * fs)) if prev_on is not None else 0)
    lo_lim = 0 if prev_on is None else prev_on + int(SKIP * fs)
    # the key: where the event is, in samples from its onset (the sample rate is measured
    # from the recording so far and moves in its third decimal every second while listening),
    # how far the data the fit sees reaches (it grows until MODEL_PAD after the window), and
    # what it is read against. A result from a rate within 1 Hz is scaled to this one.
    reach = min(lim, a1 + int(MODEL_PAD * fs), rec.gap_after(a1 - 1)) - on
    start = max(min(p0, on - int(0.05 * fs)) - int(MODEL_PAD * fs), rec.gap_before(max(0, on)), 0, lo_lim) - on
    key = (round(float(rec.t[on]), 4), a0 - on, a1 - on, p0 - on, p1 - on, max(lo_lim - on, -10 ** 9), reach, start,
           round(fs), floor.key, ctx.key(),
           float(rec.axes[0][a0]), float(rec.axes[1][a1 - 1]), float(rec.axes[2][on]))
    with _EVENTS_LOCK:
        hit = _EVENTS.pop(key, _EVENTS)
        if hit is not _EVENTS:
            _EVENTS[key] = hit              # (now the most recently used)
    if hit is not _EVENTS:
        return _rescaled(hit, fs)
    ev = _event_fit(rec, floor, on, a0, a1, p0, p1, ctx, lo_lim, lim)
    if ev is not None:
        ev["fs"] = fs
    with _EVENTS_LOCK:
        while len(_EVENTS) >= EVENTS_MAX:
            del _EVENTS[next(iter(_EVENTS))]
        _EVENTS[key] = ev
    return ev


def _rescaled(ev, fs):
    """A cached event as read at sample rate fs: every frequency and rate scales with it."""
    if ev is None or ev.get("fs") == fs:
        return ev
    r = fs / ev["fs"]
    tones = []
    for x in ev["tones"]:
        y = dict(x)
        for k in ("freq", "d", "centre", "alpha", "sigma", "sigma_a", "line"):
            if y.get(k) is not None:
                y[k] = y[k] * r
        y["tau"] = y["tau"] / r
        tones.append(y)
    f, snr = ev["spec"]
    return dict(ev, tones=tones, T=ev["T"] / r, spec=(f * r, snr), fs=fs)


def _event_fit(rec, floor, on, a0, a1, p0, p1, ctx, lo_lim=0, hi_lim=None):
    fs = rec.fs
    T = (a1 - a0) / fs
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    flo, fhi = ctx.frange or _f_range(lo, hi, None)
    # the window's spectrum, against the noise (mean 1 per bin for noise; three axes)
    nfft = _pow2(8 * fs)
    w = np.hanning(a1 - a0)
    p = 0
    for a in rec.axes:
        s = a[a0:a1]
        p = p + np.abs(np.fft.rfft((s - s.mean()) * w, nfft)) ** 2
    f = np.fft.rfftfreq(nfft, 1.0 / fs)
    snr = p / (3.0 * floor(f) * float(np.sum(w * w)))
    sel = (f >= lo) & (f <= hi)
    cells = max(1.0, (hi - lo) * T)
    thr_peak = _gamma_isf(3, MODEL_PFA / cells) / 3.0
    res = 1.0 / T
    k1 = max(1, int(res / (f[1] - f[0])))
    peaks = []
    for k in np.flatnonzero(sel & (snr > thr_peak)):
        if k - k1 >= 0 and k + k1 < len(p) and p[k] >= p[k - k1:k + k1 + 1].max():
            peaks.append(int(k))
    peaks.sort(key=lambda k: -p[k])
    # bands: around the strongest lines, then where a belt's tone or its partner would be
    # (half and twice a strong line), and any asked for
    centres = []

    def add(x):
        if lo <= x <= hi and all(abs(x - c) > MODEL_HALF - 3.0 for c in centres):
            centres.append(float(x))
    for k in peaks[:BANDS_MAX]:
        add(f[k])
    strong = [float(f[k]) for k in peaks[:BANDS_MAX] if snr[k] >= 10 ** (STRONG_DB / 10.0)]
    for x in strong:
        if lo <= x / 2.0 <= fhi:    # (below the belt's range too: a loose belt heard at its 2F)
            add(x / 2.0)
        if flo <= x <= fhi:
            add(2.0 * x)
    for x in ctx.force:
        add(x)
    thr_band = _gamma_isf(3, MODEL_PFA / max(1.0, 2.0 * MODEL_HALF * T))
    thr_new = _gamma_isf(3, EVENT_PFA / max(1.0, 2.0 * MODEL_HALF * T))
    tones = []
    for c in centres:
        band = _Band(rec, c, p0 if p0 < on else on - int(0.05 * fs), a1, lo_lim, hi_lim)
        sigma2 = max(float(floor(c)) * band.bw / fs, 1e-30)
        seeds = [(float(f[k]) - c, 5.0) for k in peaks if abs(f[k] - c) <= MODEL_HALF]
        seeds += [(x - c, 0.0) for x, _ in ctx.lines if abs(x - c) <= MODEL_HALF
                  and all(abs(x - c - d) > 0.3 for d, _ in seeds)]
        seeds += [(m - c, 1.0 / max(tau, 0.02)) for m, _, tau in ctx.modes if tau > 0
                  and abs(m - c) <= MODEL_HALF and all(abs(m - c - d) > 0.3 for d, _ in seeds)]
        if not seeds:
            seeds = [(0.0, 5.0)]
        Z = band.get(a0, a1)
        fit = _relax(Z, band, seeds, sigma2, thr_band)
        if fit is None:
            continue
        P, A, R, cov = fit
        m = Z.shape[1]
        s2 = max(sigma2, _rss(R) / max(1.0, 3.0 * m - 4.0 * len(P))) * band.over
        t = np.arange(m) / band.fz
        mine = []
        for i, (d, al) in enumerate(P):
            if abs(d) > MODEL_HALF or min(centres, key=lambda z: abs(z - c - d)) != c:
                continue            # the edge of the band: each frequency is read in the band nearest it
            if al >= 0.9 * 2.0 * math.pi * MODEL_HALF or al * JUNK_TAU >= 1.0:
                continue            # the attack's own smear through the band filter, not a tone
            level = float(np.sum(np.abs(A[i]) ** 2))
            S = float(np.sum(np.exp(-2.0 * al * t)))
            mine.append({"i": i, "d": float(d), "alpha": float(al), "freq": c + float(d), "amp": A[i],
                         "level": level, "S": S, "snr": level * S / (sigma2 * band.over),
                         "sigma": _sd(cov[2 * i, 2 * i], s2),
                         "sigma_a": _sd(cov[2 * i + 1, 2 * i + 1], s2),
                         "tau": 1.0 / al if al > 1e-6 else float("inf"), "centre": c, "m": m,
                         "s2": sigma2 * band.over})
        mine = [x for x in mine if x["snr"] >= thr_band or any(
            abs(x["freq"] - y) <= _pair_tol(y) for y in [2 * s for s in strong] + [s / 2.0 for s in strong])
            and x["snr"] >= _gamma_isf(3, MODEL_PFA / max(1.0, 2.0 * _pair_tol(x["freq"]) * T))]
        if not mine:
            continue
        # what was there before: carried on (extrapolated from just before the event) and
        # whether it was steady (the same level in both halves of the stretch before)
        cp0 = max(p0, p1 - int(CARRY * fs))
        rows = [x["i"] for x in mine]
        carried = _carry(band, P, a0, cp0, p1) if p1 - cp0 >= int(0.1 * fs) else None
        halves = _halves(band, P, p0, p1) if p1 - p0 >= int(0.2 * fs) else None
        for i, x in zip(rows, mine):
            if carried is not None:
                cv = carried[i]
                x["carry"] = float(np.sum(np.abs(cv) ** 2))
                x["new"] = float(np.sum(np.abs(x["amp"] - cv) ** 2))
            else:
                x["carry"], x["new"] = 0.0, x["level"]
            x["snr_new"] = x["new"] * x["S"] / x["s2"]
            x["snr_carry"] = x["carry"] * x["S"] / x["s2"]
            x["steady"] = False
            if halves is not None:
                h1, h2 = float(halves[0][i]), float(halves[1][i])
                m2 = band.get((p0 + p1) // 2, p1).shape[1]
                sig = _gamma_isf(3, MODEL_PFA) * x["s2"] / max(1, m2)
                x["steady"] = h1 > sig and h2 > sig and max(h1, h2) <= 1.41 * min(h1, h2)
            fresh = x["snr_new"] >= thr_new
            # (a pluck on a belt still ringing from the last one adds at least a quarter of
            # what was ringing on; the carried-on part is extrapolated to within a few %)
            x["starts"] = fresh and (x["new"] >= 10 ** (ATTACK_DB / 10.0) * x["carry"]
                                     or (not x["steady"] and x["new"] >= RENEW * x["carry"]))
            # a tone a steady line was already on: the line's frequency, from before the event
            x["mixed"] = fresh and x["steady"] and x["new"] < 10 ** (ATTACK_DB / 10.0) * x["carry"]
            if x["steady"]:
                x["line"] = _free_line(band, x["d"], p0, p1)
            # how far it stands above the noise: its fitted energy in the window against the
            # noise's (a Hann periodogram would hide a fast decay, which has its energy at the
            # window's start where Hann is near zero: -8 dB at 0.15 s, -17 dB at 0.07 s)
            x["db"] = _db(max(x["snr"], 1e-12))
            x.pop("amp")
            x.pop("i")
        tones.extend(mine)
    # the model reproduces a ringing line to about DYN of its energy (the decay is not quite
    # exponential, the extrapolation not quite exact): fainter tones beside a loud one, and new
    # parts fainter than that, are the model's own leftovers
    # (beside it: within a band's reach, MODEL_HALF + MODEL_TAPER. A loud toolhead mode
    # 60 Hz away leaves nothing at the belt's 2F, and must not hide it)
    reach = MODEL_HALF + MODEL_TAPER

    def top(x):
        return max(y["snr"] for y in tones if abs(y["freq"] - x["freq"]) <= reach)
    tops = [top(x) for x in tones]
    kept = [x for x, tp in zip(tones, tops) if x["snr"] >= DYN * tp]
    for x, tp in zip(tones, tops):
        if x["snr"] >= DYN * tp and x["snr_new"] < DYN * tp:
            x["starts"] = x["mixed"] = False
    kept.sort(key=lambda x: x["freq"])
    clipped = bool(rec.clip[a0:a1].any())
    # (the spectrum kept is where lines are looked for only, in float32: an event costs ~60 kB
    # in the cache, not 0.3 MB)
    keep = (f >= lo - 5.0) & (f <= hi + 5.0)
    return {"on": on, "a0": a0, "a1": a1, "T": T, "tones": kept, "clipped": clipped,
            "spec": (f[keep], snr[keep].astype(np.float32))}


def _events(rec, floor, q, ctx, lines=()):
    """The events in a recording: [(onset, event)], each event with a "second" mark when a
    second attack came within DEAD of its own. A candidate onset counts only if some tone
    starts there (two belts beating, or one ringing on, set off the detector but start
    nothing)."""
    fs = rec.fs
    cands = _candidates(rec, floor, q, [x for x, _ in lines])
    confirmed, second = [], set()
    for i, c in enumerate(cands):
        last = confirmed[-1] if confirmed else None
        if last is not None and c - last < int(MERGE * fs):
            continue                # rings as one with the attack before it
        # judged up to the next attack that could be one of its own (what comes after it
        # isn't this event's)
        nxt = next((x for x in cands[i + 1:] if x - c >= int(MERGE * fs)), None)
        ev = _event(rec, floor, c, nxt, last, ctx)
        if ev is None or not any(x["starts"] for x in ev["tones"]):
            continue
        if last is not None and c - last < int(DEAD * fs):
            second.add(last)
            continue
        confirmed.append(c)
    out = []
    for i, on in enumerate(confirmed):
        nxt = confirmed[i + 1] if i + 1 < len(confirmed) else None
        ev = _event(rec, floor, on, nxt, confirmed[i - 1] if i else None, ctx)
        if ev is None:
            continue
        ev = dict(ev, second=on in second)
        out.append((on, ev))
    return out


# ---------------- 5-6. labelling the tones, reading the belt ----------------
def _toolhead(x, modes):
    return any(abs(x["freq"] - m) <= g for m, g, _ in modes)


def _partner(x, tones):
    """The tone at twice x (a belt's pull at twice its tone) that starts with it, or None."""
    tol = _pair_tol(2.0 * x["freq"])
    near = [y for y in tones if y is not x and y["starts"] and abs(y["freq"] - 2.0 * x["freq"]) <= tol
            and y["db"] >= WEAK_DB]
    return max(near, key=lambda y: y["snr"]) if near else None


def _pair_class(x, y):
    """How a tone x and the tone y at twice it ring: 2 the partner clearly dies faster, and
    about as much faster as a belt's does (a belt), 1 can't tell, 0 the partner clearly rings
    longer or dies far faster than a belt's (x is not its tone). A belt's 2F comes from the
    tension change, which goes with the swing squared, so it decays at twice the rate:
    measured 1.3-3.2x on 41 real plucks (Nebula 370, 16 Oct), PAIR_RATIO allows a margin."""
    s = 2.0 * math.sqrt(x["sigma_a"] ** 2 + y["sigma_a"] ** 2)
    if x["alpha"] - y["alpha"] > s:
        return 0
    if y["alpha"] - x["alpha"] > s:
        if x["alpha"] <= 0:
            return 1
        # outside the ratio only beyond the decay rates' own +- (a 2F with another line beside
        # it is fitted with a loose decay)
        r_lo = (y["alpha"] + 2.0 * y["sigma_a"]) / max(x["alpha"] - 2.0 * x["sigma_a"], 1e-9)
        r_hi = (y["alpha"] - 2.0 * y["sigma_a"]) / (x["alpha"] + 2.0 * x["sigma_a"])
        if r_lo < PAIR_RATIO[0] or r_hi > PAIR_RATIO[1]:
            return 0
        return 2 if PAIR_RATIO[0] <= y["alpha"] / x["alpha"] <= PAIR_RATIO[1] else 1
    return 1


def _belts(ev, ctx):
    """The tones of an event that may be a belt's, each as (class, score, tone, partner):
    class 3 a partner that dies faster, 2 a partner (can't tell how it rings), 1 no partner
    but rings longer than a knock."""
    flo, fhi = ctx.frange
    out = []
    for x in ev["tones"]:
        if not x["starts"] or not SEARCH[0] <= x["freq"] <= fhi or x["alpha"] <= 2.0 * x["sigma_a"]:
            continue        # (a plucked belt dies away: a line that comes on and stays is a fan)
        y = _partner(x, ev["tones"])
        cls = 0
        if y is not None:
            pc = _pair_class(x, y)
            cls = 3 if pc == 2 else 2 if pc == 1 else 0
        if cls and _toolhead(x, ctx.modes) and (cls == 2 or _toolhead(y, ctx.modes)):
            # on a learned toolhead mode, a line at twice it may be another mode that sits
            # there: only a partner that dies like a belt's counts, and not even that when the
            # taps rang a mode at twice it too (a second mode with the same damping ratio
            # decays twice as fast as well)
            cls = 0
        if x["freq"] < flo and not (cls == 3 and flo <= y["freq"] <= fhi):
            continue        # below where the tone is looked for, unless its partner is there and it rings like a belt's tone (a loose belt)
        # alone, it has to ring longer than a click, die away within the window (a line that
        # comes on and stays is a fan) and not be on a mode the taps learned. Belts and
        # toolheads both ring 0.05-0.3 s, so ring time can't tell them apart: a lone tone is
        # only taken for the belt where a plucked belt with a partner, or the belt's last
        # reading, agrees with it (analyse: _lone_support)
        if cls == 0 and y is None and x["tau"] >= CLICK_TAU and not _toolhead(x, ctx.modes):
            cls = 1
        if cls:
            score = math.log1p(x["snr"]) + (math.log1p(y["snr"]) if y is not None else 0.0)
            out.append((cls, score, x, y))
    return out


def _at_half(x, f):
    """Tone x sits where tone f's note would be if f were its 2F: a belt's 2F is sharp (0.2-1 %,
    stiffness) and a weak F is fitted loosely, so wider than a partner is looked for."""
    return abs(2.0 * x - f) <= max(PAIR_HZ, HALF_SHARE * f)


def _pick(ev, ctx, prev=None):
    """The belt's tone in an event: (tone, partner, class, doubt) or (None, None, 0, None). The
    best kind of evidence wins; down an octave while the tone is itself the partner of a lower
    tone that rings like a belt's (a string's tone is the lowest of its series); between
    octaves that can't be told apart, the one nearer the belt's last reading. doubt: the Hz
    of a tone at half the pick that may be its note (it starts with it, and how the two
    decay doesn't say it isn't): then neither can be read."""
    every = cands = _belts(ev, ctx)
    if not cands:
        return None, None, 0, None
    # a tone the judge would refuse anyway (read too unsurely, or a click) doesn't outrank one
    # it would take: a junk tone with a partner must not hide the belt
    sure = [c for c in cands if c[2]["sigma"] <= SIGMA_MAX and c[2]["tau"] >= CLICK_TAU]
    cands = sure or cands
    top = max(c[0] for c in cands)
    best = max((c for c in cands if c[0] == top), key=lambda c: c[1])
    went_down = False
    while True:
        # down an octave to a tone that is a belt's on its own: a partner dying like a belt's
        # (class 3), there at all (WEAK_DB: where it is looked for is known, at half the pick)
        # and itself dying away. Its partner may be fitted as a neighbour of the best tone (2F
        # crowded by the other belt's), and it may be read too weakly or unsurely to count:
        # then the judge refuses it, rather than the 2F being counted (a sensor that hears 2F
        # 20-30 dB louder than F does this: the Beacon twin recordings; a loose belt too)
        f = best[2]["freq"]
        lower = [c for c in every if c[0] == 3 and c[2]["freq"] < 0.75 * f
                 and _at_half(c[2]["freq"], f) and c[2]["db"] >= WEAK_DB
                 and c[2]["alpha"] > 2.0 * c[2]["sigma_a"] and not _toolhead(c[2], ctx.modes)]
        if not lower:
            break
        low = max(lower, key=lambda c: c[1])
        if best[3] is not None and best[0] == 3 and low[2]["freq"] < ctx.frange[0] \
                and not (prev and abs(math.log(low[2]["freq"] / prev)) < OCT_NEAR):
            # the pick's own partner dies like a belt's too (X, 2X, 4X), and the lower tone is
            # below where the belt was looked for: a frame mode the taps didn't learn, rung by
            # the pluck at half the belt's note, rings just the same (review 7: 38 of 120 normal
            # belts read at half). Neither is read, unless the belt's last reading was there.
            # (Below the band only: a loose belt is then refused 9 times in 113, not 35)
            return best[2], best[3], best[0], low[2]["freq"]
        best = low
        went_down = True
    # a learned toolhead mode whose partner could be the belt: the toolhead ringing at half the
    # belt (the pluck rings it too), or the belt's note on the toolhead's: the tones can't tell
    # which (review 6; the belt's own 4F that would show it is often just outside the partner
    # tolerance, review 7), so neither is read
    if _toolhead(best[2], ctx.modes) and best[3] is not None \
            and ctx.frange[0] <= best[3]["freq"] <= ctx.frange[1]:
        up = next((c for c in every if c[2] is best[3]), None)
        if up is None:
            return best[3], None, 2, best[2]["freq"]
        return up[2], up[3], up[0], best[2]["freq"]
    # a tone at half the pick that starts with it and may be its note (its decay doesn't say
    # otherwise) but isn't sure enough to go down to: which octave is the belt can't be told
    f = best[2]["freq"]
    # (any tone kept there: a tone at half a strong line was kept only above the false-alarm
    # level of a known position; new with this pluck, not a steady line, and dying away)
    doubt = [z for z in ev["tones"] if z is not best[2] and not z.get("steady") and not z.get("mixed")
             and z.get("new", 0.0) >= RENEW * z.get("carry", 0.0) and z["alpha"] > 0.0
             and z["freq"] < 0.75 * f and _at_half(z["freq"], f)
             and _pair_class(z, best[2]) != 0]
    doubt = max(doubt, key=lambda z: z["snr"]) if doubt else None
    if doubt is not None and prev:
        # the belt's last reading settles it for the lower one when it is near it (a tension
        # change between rounds is a few %; the other octave is then 2x away). Never for the
        # upper: a reading of 2F stored once would confirm itself every round after
        if abs(math.log(doubt["freq"] / prev)) < OCT_NEAR:
            low = [c for c in every if c[2] is doubt]
            if low:
                best, doubt, went_down = low[0], None, True
    doubt = None if doubt is None else doubt["freq"]
    # (once a lower tone rang as this one's tone, or this one's partner dies like a belt's, the
    # octaves were told apart: the last reading must not take the 2F back)
    if prev and not went_down and best[0] < 3 and doubt is None:
        for c in cands:
            if c is best or c[0] != best[0] or c[1] < best[1] - OCT_TIE:
                continue
            # (down only, for the same reason: up an octave is the tone's own partner)
            r = c[2]["freq"] / best[2]["freq"]
            if abs(r - 0.5) < 0.008 and \
                    abs(math.log(c[2]["freq"] / prev)) < abs(math.log(best[2]["freq"] / prev)):
                best = c
    return best[2], best[3], best[0], doubt


def _hider(x, ev, ctx):
    """A steady line that hides tone x: closer than the window resolves, with more than
    1/HIDE_DB of the tone's energy in the window. Returns its Hz or None."""
    res = 1.0 / ev["T"]
    lim = x["snr_new"] / 10 ** (HIDE_DB / 10.0)
    if x["steady"] and x["carry"] * x["m"] / x["s2"] >= lim:
        return x.get("line", x["freq"])        # the line the tone sat on (a fan does not decay)
    for y in ev["tones"]:
        if y is not x and y["steady"] and not y["starts"] and abs(y["freq"] - x["freq"]) < res \
                and y["level"] * y["m"] / y["s2"] >= lim:
            return y.get("line", y["freq"])
    for z, lv in ctx.lines:
        if abs(z - x["freq"]) < res and lv * x["m"] / x["s2"] >= lim:
            return z
    return None


def _lone_tau(ctx):
    """How long a tone with no partner must ring to be taken for a belt: a click's ring
    (CLICK_TAU) once the taps have shown where the toolhead rings, else a knock's (KNOCK_TAU)."""
    return CLICK_TAU if ctx.modes else KNOCK_TAU


def _knocklike(x, ev, ctx):
    """A tone a knock rang: no partner, and the toolhead's own (a mode the taps learned) or
    dying faster than a belt."""
    return _partner(x, ev["tones"]) is None and (_toolhead(x, ctx.modes) or x["tau"] < _lone_tau(ctx))


# ---------------- 7. judging a pluck ----------------
def _smeared(x, y, ev):
    """A second pluck too soon after the first for the onset detector to see (the belt still
    rings louder than it adds): one decaying tone can't fit a level that jumps, so the fit
    puts new tones on both sides of it, about 1/(time between the plucks) apart. A tone with
    new tones within SMEAR_HZ on both sides (not steady lines, not its partner, at least
    SMEAR_SHARE of its strength, ringing at least SMEAR_TAU like the belt they come from)
    isn't read: which of them is the belt? Two belts ringing together put one tone beside it,
    not one on each side; a pluck's own short transients (0.03-0.09 s on the twin recordings)
    flank it too, and don't count."""
    lo = hi = False
    res = 0.5 / ev["T"]
    for z in ev["tones"]:
        if z is x or z is y or not z["starts"] or z.get("steady") or z["snr"] < SMEAR_SHARE * x["snr"] \
                or z["tau"] < SMEAR_TAU:
            continue
        d = z["freq"] - x["freq"]
        if res < abs(d) <= SMEAR_HZ:
            lo, hi = lo or d < 0, hi or d > 0
    return lo and hi


def _judge(rec, on, ev, ctx, prev):
    """One pluck: its reading and whether it counts (and why not)."""
    fs = rec.fs
    pk = {"t": round(float(rec.t[on] - rec.t[0]), 2), "freq": None, "db": None, "db2": None,
          "ok": False, "why": "", "sigma": None, "tau": None}
    flo, fhi = ctx.frange
    tones = ev["tones"]
    x, y, cls, doubt = _pick(ev, ctx, prev)
    if x is None:
        mixed = [z for z in tones if (z.get("mixed") or z["steady"]) and flo <= z["freq"] <= fhi]
        hid = None
        for z in sorted(mixed, key=lambda z: -z["snr_new"]):
            hid = _hider(z, ev, ctx)
            if hid is not None:
                pk.update(freq=round(z["freq"], 2), db=round(z["db"], 1))
                break
        # (a knock is told by what rang where the belt's tone is looked for: a click far
        # above it says nothing about the belt)
        starts = [z for z in tones if z["starts"] and flo <= z["freq"] <= fhi]
        if hid is not None:
            pk["hidden"] = round(hid, 1)
            pk["why"] = "hidden by a steady hum at %.0f Hz" % hid
        elif ev.get("second"):
            pk["why"] = "two plucks at once"      # (a second attack cut the window short)
        elif starts and _knocklike(max(starts, key=lambda z: z["snr_new"]), ev, ctx):
            pk["why"] = "a knock, not a belt"
        elif not starts and any(z["steady"] for z in tones if flo <= z["freq"] <= fhi):
            z = max((z for z in tones if z["steady"] and flo <= z["freq"] <= fhi), key=lambda z: z["snr"])
            pk["steady"] = round(z.get("line", z["freq"]), 1)
            pk["why"] = "steady hum, not a pluck"
        else:
            pk["why"] = "no belt tone"
        return pk
    d1, d2 = x["db"], (y["db"] if y is not None else None)
    pk.update(freq=round(x["freq"], 2), db=round(d1, 1), db2=None if d2 is None else round(d2, 1),
              sigma=round(x["sigma"], 3) if math.isfinite(x["sigma"]) else None,
              tau=None if math.isinf(x["tau"]) else round(x["tau"], 3))
    if cls == 1:
        # see _lone_support. (Without taps learned a lone tone dying faster than a knock's is
        # only "short": real belts ring 0.08-0.25 s, so a counted pluck beside it may vouch for it)
        pk["lone"] = ("knock" if _toolhead(x, ctx.modes) or x["tau"] < CLICK_TAU else "short") \
            if _knocklike(x, ev, ctx) else "lone"
    hid = _hider(x, ev, ctx)
    res = 1.0 / ev["T"]
    # two tones closer than the window resolves (both fitted: the fit keeps them apart and its
    # +- says how well) are refused only when they are about as strong: which one was plucked?
    # (and only when the other could be the plucked belt as well as this one: as good a kind of
    # evidence. A tone without a partner beside a belt with one is not the belt plucked)
    rivals = {id(c[2]) for c in _belts(ev, ctx) if c[0] >= cls}
    beside = [z for z in tones if z is not x and z is not y and z["starts"] and abs(z["freq"] - x["freq"]) < res
              and (z["snr_new"] * 10 ** (BESIDE_DB / 10.0) >= x["snr_new"] and id(z) in rivals
                   or z["snr_new"] > x["snr_new"])]      # (or louder: the plucked belt is the loud one)
    if hid is not None:
        pk["hidden"] = round(hid, 1)
        pk["why"] = "hidden by a steady hum at %.0f Hz" % hid
    elif ev.get("second"):
        pk["why"] = "two plucks at once"      # (the second attack cut the window short: it explains the rest)
    elif _smeared(x, y, ev):
        pk["why"] = "two plucks at once (the tone is smeared)"
    elif doubt is not None:
        pk["octave"] = round(doubt, 2)
        pk["why"] = "can't tell the belt's note from twice it (%.1f or %.1f Hz): pluck again" % (doubt, x["freq"])
    elif (d2 is None and d1 < STRONG_DB) or (d2 is not None and (max(d1, d2) < STRONG_DB or min(d1, d2) < WEAK_DB)):
        pk["why"] = "too weak"
    elif x["sigma"] > SIGMA_MAX:
        pk["why"] = "not clear enough (+-%.2f Hz)" % x["sigma"] if math.isfinite(x["sigma"]) else "not clear enough"
    elif beside:
        pk["why"] = "another tone beside it at %.1f Hz (the other belt ringing along?)" % beside[0]["freq"]
    elif ev["clipped"]:
        pk["why"] = "the sensor clipped: pluck more gently"
    else:
        pk["ok"] = True
    return pk


def _lone_support(plucks, prev, hears=None, target=None):
    """A tone with no partner at twice it counts only where something else says it is the
    belt: a counted pluck with a partner within SPREAD of it, or the belt's last reading.
    On a sensor that can't hear twice it (no pluck has a partner there) it counts when it
    rings longer than a knock and sits within SPAN of the target: a knock on a frame mode
    can ring as long, so further off it can't be told from one. Otherwise it is a knock
    (dying fast, or on the toolhead) or not sure."""
    sure = [p["freq"] for p in plucks if p["ok"] and not p.get("lone")] + ([prev] if prev else [])
    for p in plucks:
        # (a knock is refused however near the belt it rang: an unlearned frame mode within
        # SPREAD would be averaged in)
        if not (p["ok"] and p.get("lone")) or (p["lone"] != "knock" and any(abs(p["freq"] - f) <= SPREAD for f in sure)):
            continue
        deaf = hears is not None and 2.0 * p["freq"] > 0.9 * hears
        if deaf and p["lone"] == "lone" and (p.get("tau") or 0.0) >= KNOCK_TAU and target is not None \
                and abs(p["freq"] - target) <= SPAN * target:
            continue
        p["ok"] = False
        if p["lone"] in ("knock", "short"):
            p["why"] = "a knock, not a belt"
        elif deaf:
            p["why"] = "can't tell from a knock: the sensor doesn't hear twice the tone"
        else:
            p["why"] = "no tone at twice it: pluck again"


def _hums(events, quiet_at):
    """Lines that hum through the listen but weren't in the background (a fan that came on
    later): steady at most of the events. Returns their Hz."""
    seen = []
    for on, ev in events:
        for x in ev["tones"]:
            if x.get("line") is not None and (x["steady"] and not x["starts"] or x.get("mixed")):
                seen.append((x["line"], on))
    seen.sort()
    groups = []
    for fr, on in seen:
        if groups and fr - groups[-1][-1][0] <= 0.5:
            groups[-1].append((fr, on))
        else:
            groups.append([(fr, on)])
    need = max(2, (len(events) + 1) // 2)
    out = []
    for g in groups:
        if len({on for _, on in g}) >= need:
            fr = float(np.median([x for x, _ in g]))
            if all(abs(fr - z) > 1.0 for z in quiet_at):
                out.append(round(fr, 1))
    return out


def _modes(avoid, taus=False):
    """The toolhead's own lines as (Hz, guard Hz[, tau s]): from a tap's "ring" (each line
    with its own guard) or plain Hz (the least guard)."""
    out = []
    for m in avoid or ():
        if isinstance(m, dict):
            out.append((float(m["freq"]), float(m.get("guard", TAP_GUARD)), float(m.get("tau") or 0.0)))
        else:
            out.append((float(m), TAP_GUARD, 0.0))
    return out if taus else [(m, g) for m, g, _ in out]


def _ctx_modes(modes):
    """The learned modes as the events are read against them. Every mode keeps its guard
    (at least TAP_GUARD): a long-ringing mode is fitted sharply, but it moves by 1.5 Hz from
    session to session (Nebula taps, 16-17 Oct: 105.0-106.5 Hz)."""
    return [(m, g, tau) for m, g, tau in modes]


def _clusters(fr):
    """Groups of readings within SPREAD of each other, largest first: (centre, members)."""
    best = []
    for x in fr:
        mem = [y for y in fr if abs(y - x) <= SPREAD]
        best.append((float(np.median(mem)), mem))
    best.sort(key=lambda c: -len(c[1]))
    return best


def _background(rec, q):
    """The noise floor, the steady lines of the background (Hz, level), and the stretch it
    came from (None: the recording itself)."""
    fs = rec.fs
    if q is not None:
        floor = _Floor(q, fs)
        if floor.ok:
            return floor, _lines(q, fs, floor)
    floor = _Floor(rec.axes, fs, pct=20)     # plucks are loud now and then: a low percentile
    return floor, []


def analyse(t, ax, ay, az, target=None, expect=None, quiet=None, avoid=(), other=None, prev=None, free=False,
            hears=None):
    """Belt tone from a recording with several plucks. quiet: (ax, ay, az) of a short
    recording without plucks, to tell fans and motors from the belt. target: the target
    tone (bounds where the tone is looked for). avoid: the toolhead's own ringing (the tap's
    "ring", or plain Hz). other: the other belt's last reading. prev: this belt's last
    reading (decides between a tone and its octave when nothing else can). hears: the highest
    frequency the sensor hears (its filter): a tone whose twice is above it has no partner to
    show, so a lone tone counts there without one.

    Each pluck is accepted or thrown out; only accepted plucks are averaged. Returns a
    dict: freq (None unless the series is good), plucks (every pluck heard: t, freq, dB at
    the tone and at twice it, sigma, tau, ok, why), used, heard, verdict (good, again,
    spread, spans, none, free), note, hum."""
    for name, value in (("target", target), ("other", other), ("prev", prev), ("hears", hears)):
        if value is not None and (not math.isfinite(value) or value <= 0):
            raise ValueError("%s frequency must be positive and finite" % name)
    if expect is not None and (isinstance(expect, bool) or not isinstance(expect, (int, np.integer)) or expect < 1):
        raise ValueError("expected pluck count must be a positive integer")
    avoid = tuple(avoid)
    try:
        modes = _modes(avoid, taus=True)
    except (TypeError, ValueError, KeyError) as error:
        raise ValueError("invalid toolhead modes") from error
    if any(not math.isfinite(freq) or freq <= 0
           or not math.isfinite(guard) or guard < 0
           or not math.isfinite(tau) or tau < 0 for freq, guard, tau in modes):
        raise ValueError("toolhead modes require a positive finite frequency and finite nonnegative guard/tau")
    st = _setup(t, ax, ay, az, quiet)
    metadata = {"sample_count": len(t), "sample_rate": None, "duration": 0.0}
    hum_at, quiet_at = [], []

    def out(verdict, note, plucks=(), freq=None, used=0):
        frequencies = [p["freq"] for p in plucks if p["ok"] and p["freq"] is not None]
        # a new hum on the belt's note (within SPAN of the target or of its last reading) hides
        # the belt: the page says to turn fans off. Hums elsewhere are harmless.
        # Only said when most plucks failed: plucks heard clearly but too few are not a fan's doing.
        near = [x for x in (target, prev) if x]
        hum = [x for x in hum_at if any(abs(x - y) <= SPAN * y for y in near)] if near else \
            [x for x in hum_at if x <= F_MAX]
        # a steady line that hid most plucks: the belt sits on it
        # (each pluck reads the line afresh, and a fan wanders a little: within 0.5 Hz is one line)
        hid = sorted(pk.get("hidden") for pk in plucks if pk.get("hidden"))
        for x in hid:
            same = [y for y in hid if abs(y - x) <= 0.5]
            fr = round(float(np.median(same)), 1)
            if 2 * len(same) >= len(plucks) and all(abs(fr - y) > 1.0 for y in hum):
                hum.append(fr)
        # or one in the belt's band (within MODEL_HALF of the target or its last reading) where
        # most plucks heard only it: a loud fan there drowns the belt (they come up as "steady
        # hum, not a pluck")
        st = sorted(pk.get("steady") for pk in plucks if pk.get("steady"))
        for x in st:
            same = [y for y in st if abs(y - x) <= 0.5]
            fr = round(float(np.median(same)), 1)
            if 2 * len(same) >= len(plucks) and any(abs(fr - y) <= MODEL_HALF for y in near) \
                    and all(abs(fr - y) > 1.0 for y in hum):
                hum.append(fr)
        # or one a few Hz from what most plucks read, when they were read too unsurely to count
        # (a loud fan in the belt's band leaves the fit a noisy leftover)
        vague = [pk["freq"] for pk in plucks if pk["why"].startswith("not clear") and pk["freq"] is not None]
        if freq is None and 2 * len(vague) >= len(plucks) > 0:
            mid = float(np.median(vague))
            for x in quiet_at:
                if abs(x - mid) <= 0.5 * MODEL_HALF and all(abs(x - y) > 1.0 for y in hum):
                    hum.append(x)
        # or one right beside the few plucks that got through it (the rest were lost in it)
        if freq is None and len(frequencies) < MIN_GOOD:
            for x in quiet_at + list(hum_at):
                if any(abs(y - x) <= 2.0 or abs(y - 2 * x) <= 2.0 for y in frequencies) \
                        and all(abs(x - y) > 1.0 for y in hum):
                    hum.append(x)
        elif 2 * len(frequencies) >= len(plucks) and plucks:
            hum = []
        if hum and freq is None:
            note = "steady hum at %s Hz on the belt's note (a fan?): turn fans off, then measure again" % (
                " and ".join("%.0f" % x for x in hum))
        return {"freq": freq, "plucks": list(plucks), "used": used, "heard": len(plucks), "hum": hum,
                "verdict": verdict, "note": note, "metadata": dict(metadata),
                "spread": round(max(frequencies) - min(frequencies), 3) if frequencies else None,
                "stddev": round(float(np.std(frequencies)), 3) if frequencies else None}
    if st is None:
        return out("none", "no data from the accelerometer")
    rec, q = st
    fs = rec.fs
    metadata.update(sample_rate=round(float(fs), 3), duration=round(float(rec.t[-1] - rec.t[0]), 3))
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * fs)
    floor, lines = _background(rec, q)
    if not floor.ok:
        return out("none", "no pluck heard")
    quiet_at = [x for x, _ in lines]
    ctx = _Ctx(lines, _ctx_modes(modes), _f_range(lo, hi, target))
    events = _events(rec, floor, q, ctx, lines)
    if not events:
        return out("none", "no pluck heard")
    hum_at = _hums(events, quiet_at)
    plucks = [_judge(rec, on, ev, ctx, prev) for on, ev in events]
    _lone_support(plucks, prev, hears, target)
    ok = [x for x in plucks if x["ok"]]
    if other and ok:
        med = float(np.median([x["freq"] for x in ok]))
        for x in ok:
            if abs(x["freq"] - other) <= OTHER_GUARD and abs(x["freq"] - med) > 1.5:
                x["ok"], x["why"] = False, "the other belt was ringing along"
        ok = [x for x in plucks if x["ok"]]
    if not plucks:
        return out("none", "no clear pluck heard")
    if free:
        # free tune: the belts change between plucks on purpose, so each clear pluck stands alone
        return out("free" if ok else "none", "%d of %d plucks clear" % (len(ok), len(plucks)), plucks,
                   freq=ok[-1]["freq"] if ok else None, used=len(ok))
    if len(ok) < MIN_GOOD:
        return out("again", "%d of %d plucks clear" % (len(ok), len(plucks)), plucks, used=len(ok))
    # plucks far from the rest rang on another stretch of belt: left out if most agree,
    # else no reading
    fr = [x["freq"] for x in ok]
    cl = _clusters(fr)
    c, mem = cl[0]
    lo, hi = min(fr), max(fr)
    # (counted or not: a pluck refused as unclear that read half a counted reading is the
    # belt's note heard faintly, and the count may be its 2F; review 7)
    seen = [x["freq"] for x in plucks if x["freq"] is not None and not x.get("hidden")]
    below = [y for y in seen if any(abs(x / y - 2.0) < 0.06 for x in fr)]
    if below:
        lo, hi = min(below), max(fr)
    if below or (hi > 1.9 * lo and any(abs(y / x - 2.0) < 0.06 for x in fr for y in fr)):
        # some read the note, some twice it: which is the belt can't be told by a vote
        return out("spans", "the plucks read at two octaves (%.1f and %.1f Hz): pluck again" % (lo, hi),
                   plucks, used=len(ok))
    if len(mem) >= MIN_GOOD and 2 * len(mem) > len(fr):
        for x in ok:
            if abs(x["freq"] - c) > SPAN * c:
                x["ok"], x["why"] = False, "rang at %.1f Hz instead of %.1f Hz" % (x["freq"], c)
        ok = [x for x in ok if x["ok"]]
    elif max(fr) - min(fr) > SPAN * float(np.median(fr)):
        return out("spans", "the plucks don't agree (%.1f-%.1f Hz)" % (min(fr), max(fr)),
                   plucks, used=len(ok))
    # the rest must agree within SPREAD; one stray among agreeing ones is thrown out
    stray = False
    while True:
        fr = [x["freq"] for x in ok]
        med = float(np.median(fr))
        far = [x for x in ok if abs(x["freq"] - med) > SPREAD]
        if not far:
            break
        if stray or len(far) > 1 or len(ok) - 1 < MIN_GOOD:
            return out("spread", "the plucks disagree (%.1f-%.1f Hz)" % (min(fr), max(fr)),
                       plucks, used=len(ok))
        far[0]["ok"], far[0]["why"] = False, "far from the others"
        ok = [x for x in ok if x["ok"]]
        stray = True
    if expect and len(ok) > expect:
        # more good plucks than asked for: the first ones count
        for x in ok[expect:]:
            x["ok"], x["why"] = False, "more than needed"
        ok = ok[:expect]
        fr = [x["freq"] for x in ok]
    return out("good", "%d of %d plucks counted" % (len(ok), len(plucks)), plucks,
               freq=round(float(np.mean(fr)), 1), used=len(ok))


# ---------------- 9. taps: the toolhead's own ringing ----------------
def _width(f, p, k):
    """Full width (Hz) of the line at bin k where it is 6 dB down (a quarter of its power)."""
    lvl = p[k] / 4.0
    i = k
    while i > 0 and p[i] > lvl:
        i -= 1
    j = k
    while j < len(p) - 1 and p[j] > lvl:
        j += 1
    return (j - i) * (f[1] - f[0])


def _tap_lines(ev, ctx):
    """The toolhead's own lines in one tap's event, strongest first: tones that start at the
    tap, ring out (not steady), stand clearly above the noise and are no belt (no partner at
    twice them, not a partner of a tone at half them). Returns (ring, hidden Hz or None)."""
    f, snr = ev["spec"]
    tones = ev["tones"]
    ring, hidden = [], None
    for x in sorted(tones, key=lambda z: -z["db"]):
        if len(ring) >= 5:
            break
        if not (x["starts"] or x.get("mixed")) or x["db"] < STRONG_DB or not SEARCH[0] <= x["freq"]:
            continue
        h = _hider(x, ev, ctx)
        if h is not None:
            hidden = hidden or h
            continue
        if not x["starts"] or x["alpha"] <= 0.05:
            continue                  # doesn't ring out: a steady line
        # a belt the tap set ringing: its tone (with a partner at twice it), or twice its tone.
        # Only where a belt's tone can be (around the target): a frame mode at half the
        # toolhead's note doesn't make the toolhead a belt
        # The tap's strongest line is the toolhead's own, whatever rings with it: a tap hits
        # the toolhead, and a mode with another at twice it (the same damping ratio decays
        # twice as fast, like a belt's 2F) must still be learned, both lines of it
        blo, bhi = getattr(ctx, "belt", None) or (0.0, float("inf"))
        y = _partner(x, tones)
        if ring:
            if y is not None and blo <= x["freq"] <= bhi and _pair_class(x, y) >= 1:
                continue
            if any(z["starts"] and blo <= z["freq"] <= bhi and abs(x["freq"] - 2.0 * z["freq"]) <= _pair_tol(x["freq"])
                   and _pair_class(z, x) >= 1 and z["db"] >= WEAK_DB for z in tones if z is not x):
                continue
        if any(abs(x["freq"] - r["freq"]) <= 3 * TAP_GUARD for r in ring):
            continue
        k = min(len(f) - 1, max(0, int(round((x["freq"] - f[0]) / (f[1] - f[0])))))
        k = max(0, k - 3) + int(np.argmax(snr[max(0, k - 3):k + 4]))
        wd = _width(f, snr, k)
        ring.append({"freq": round(x["freq"], 1), "tau": round(float(x["tau"]), 2), "width": round(float(wd), 1),
                     "guard": round(float(min(GUARD_MAX, max(TAP_GUARD, wd / 2.0))), 1)})
        if len(ring) == 1 and y is not None and y["alpha"] > 0.05 and y["db"] >= WEAK_DB:
            k = min(len(f) - 1, max(0, int(round((y["freq"] - f[0]) / (f[1] - f[0])))))
            k = max(0, k - 3) + int(np.argmax(snr[max(0, k - 3):k + 4]))
            wd = _width(f, snr, k)
            ring.append({"freq": round(y["freq"], 1), "tau": round(float(y["tau"]), 2), "width": round(float(wd), 1),
                         "guard": round(float(min(GUARD_MAX, max(TAP_GUARD, wd / 2.0))), 1)})
    return ring, hidden


def _tap_setup(t, ax, ay, az, quiet, target=None):
    st = _setup(t, ax, ay, az, quiet)
    if st is None:
        return None
    rec, q = st
    floor, lines = _background(rec, q)
    if not floor.ok:
        return None
    lo, hi = SEARCH[0], min(SEARCH[1], 0.45 * rec.fs)
    ctx = _Ctx(lines, (), (lo, hi))
    ctx.belt = _f_range(lo, hi, target)       # where a belt the tap sets ringing can have its tone
    return rec, q, floor, lines, ctx


def analyse_tap(t, ax, ay, az, quiet=None, target=None):
    """The toolhead's own ringing from a light tap: the strongest lines (up to 5; a hard tap
    brings out more of them) that stand clearly above the noise and are not a belt ringing
    along (no line at twice them or at half them that rings like a belt's).

    Toolheads differ a lot: plastic and loosely mounted ones ring briefly in broad lines,
    stiff all metal ones ring long in sharp lines. How long a line rings doesn't tell it from
    a belt (both may ring 0.1-0.3 s), so the harmonics tell. Each line's ring time (fitted)
    and width are learned, and each line is guarded as wide as it is.

    Returns modes (Hz), ring (per line: freq, tau = s to 1/e, width = Hz at -6 dB, guard =
    Hz either side ignored as a belt tone), tau (the longest), note."""
    st = _tap_setup(t, ax, ay, az, quiet, target)
    if st is None:
        return {"modes": [], "ring": [], "tau": None, "note": "no data from the accelerometer"}
    rec, q, floor, lines, ctx = st
    events = _events(rec, floor, q, ctx, lines)
    if not events:
        return {"modes": [], "ring": [], "tau": None, "note": "no tap heard"}
    ring, _ = _tap_lines(events[0][1], ctx)
    longest = max((r["tau"] for r in ring), default=None)
    return {"modes": [r["freq"] for r in ring], "ring": ring, "tau": longest,
            "note": "" if ring else "the tap rang nothing clearly"}


# ---------------- the toolhead's fingerprint: three light taps on the same spot ----------------
# One tap finds the toolhead's note; three that agree show it is steady and that it is the
# toolhead, not something else that happened to ring. A tap only counts when it is heard,
# alone (not inside the last tap's window: a double tap muddles the ringing) and rings
# loudest at the same note as the first good tap, within that note's own width (the same
# spot: another spot brings out other notes). No fixed numbers: each toolhead sets its own.
# How hard is not judged: even a light finger tap on a stiff extruder hits a +-16 g sensor's
# limit for an instant (seen on a Nebula 370), and the ringing after it, which is all that is
# used, is fine.
def analyse_taps(t, ax, ay, az, quiet=None, need=TAP_NEED, target=None):
    """Judge each tap in a recording and learn the toolhead's fingerprint from the first
    `need` good ones. Returns taps (each: t, ok, why, freq = its loudest note), good (count),
    bad (count), main (the toolhead's note: freq, guard, ...), modes, ring and tau (as
    analyse_tap), and note. A tap whose window is not complete yet is left for later."""
    out = {"taps": [], "good": 0, "bad": 0, "main": None, "modes": [], "ring": [], "tau": None, "note": ""}
    st = _tap_setup(t, ax, ay, az, quiet, target)
    if st is None:
        return out
    rec, q, floor, lines, ctx = st
    fs = rec.fs
    events = _events(rec, floor, q, ctx, lines)
    good, ref, prev = [], None, None
    for i, (on, ev) in enumerate(events):
        last = i + 1 == len(events)
        if last and on + int((SKIP + WINDOW) * fs) > rec.n:
            break                     # still ringing at the end: judged on the next look
        tap = {"t": round(float(rec.t[on] - rec.t[0]), 2), "ok": False, "why": "", "freq": None}
        if (prev is not None and on - prev < (SKIP + WINDOW) * fs) or ev.get("second"):
            tap["why"] = "two taps at once"
        else:
            ring, hidden = _tap_lines(ev, ctx)
            if not ring:
                tap["why"] = ("hidden by a steady hum at %.0f Hz" % hidden) if hidden is not None else "too soft"
            else:
                tap["freq"] = ring[0]["freq"]
                if ref is not None and abs(ring[0]["freq"] - ref["freq"]) > ref["width"] / 2.0:
                    tap["why"] = "another spot"
                else:
                    ref = ref or ring[0]
                    good.append(ring)
                    tap["ok"] = True
        prev = on
        out["taps"].append(tap)
        if len(good) >= need:
            break
    out["good"] = len(good)
    out["bad"] = sum(1 for x in out["taps"] if not x["ok"])
    if not good:
        out["note"] = "no tap heard" if not out["taps"] else "no good tap"
        return out
    # the fingerprint: the main note, and the other lines that rang in at least two good taps
    rings = sorted(((r, k) for k, g in enumerate(good) for r in g), key=lambda x: x[0]["freq"])
    groups = []
    for r, k in rings:
        if groups and r["freq"] - groups[-1][-1][0]["freq"] <= 2 * TAP_GUARD:
            groups[-1].append((r, k))
        else:
            groups.append([(r, k)])
    ring = []
    for g in groups:
        has_main = any(r is gd[0] for r, k in g for gd in good)
        if len({k for _, k in g}) < min(2, len(good)) and not has_main:
            continue
        fr = float(np.median([r["freq"] for r, _ in g]))
        ring.append({"freq": round(fr, 1), "tau": max(r["tau"] for r, _ in g),
                     "width": max(r["width"] for r, _ in g), "guard": max(r["guard"] for r, _ in g)})
    mains = [g[0]["freq"] for g in good]
    mf = float(np.median(mains))
    main = min(ring, key=lambda r: abs(r["freq"] - mf))
    ring.sort(key=lambda r: r is not main)
    out.update(main=main, ring=ring, modes=[r["freq"] for r in ring],
               tau=max(r["tau"] for r in ring))
    return out


def toolhead_knock(t, ax, ay, az, at, main, belt, quiet=None, until=None):
    """Was the event at `at` (s after t[0]) a knock on the toolhead rather than a pluck? Its
    note (main, from the setup taps) must start there, be clearly heard (STRONG_DB over the
    noise) and be louder than the belt's note (belt: Hz, belt A's reading). until: when the
    next event starts (s), so a double tap's second knock isn't heard in the first one's
    window. Returns (yes, dB of the toolhead note, dB of the belt note)."""
    st = _tap_setup(t, ax, ay, az, quiet)
    if st is None or not main:
        return False, None, None
    rec, q, floor, lines, ctx = st
    fs = rec.fs
    t = rec.t
    on = int(np.searchsorted(t, t[0] + at))
    lim = int(np.searchsorted(t, t[0] + until)) if until is not None else None
    if on >= rec.n:
        return False, None, None
    mf, mg = float(main["freq"]), float(main["guard"])
    force = [mf] + ([float(belt)] if belt else [])
    ev = _event(rec, floor, on, lim, None, _Ctx(lines, (), ctx.frange, force))
    if ev is None:
        return False, None, None
    at_main = [x for x in ev["tones"] if abs(x["freq"] - mf) <= mg]
    if not at_main:
        return False, None, None
    dm = max(x["db"] for x in at_main)
    # a knock starts the toolhead's note; a fan humming on (or beside) that note was there
    # just before the event as well: no attack, no knock
    if not any(x["starts"] for x in at_main):
        return False, round(dm, 1), None
    dm = max(x["db"] for x in at_main if x["starts"])
    # the belt rings as a line of its own, within SPREAD of its reading. A belt inside the
    # toolhead's guard can't be told apart.
    do = -99.0
    if belt and abs(belt - mf) > mg + SPREAD:
        for x in ev["tones"]:
            if x["starts"] and abs(x["freq"] - belt) <= SPREAD:
                do = max(do, x["db"])
    return dm >= STRONG_DB and dm > do, round(dm, 1), round(do, 1)


def plan(b):
    """What to tell the user after both belts were measured. b is the belt state
    (target, tol, A, B = belt A and B (the user picks which is which), last_belt: the one changed last); returns
    (advice, updates for b). Which belt is which doesn't matter: they are tensioned
    equally.
    Just which way, and one belt per round, taking turns, so the two are brought up
    together and never end far apart."""
    tgt, tol = b["target"], b["tol"]
    fa = (b.get("A") or {}).get("freq")
    fb = (b.get("B") or {}).get("freq")
    upd = {}
    if fa is None or fb is None:
        return "", upd
    need = b.get("plucks", 5)
    def enough(r):
        return r.get("enough", (r.get("used") or 0) >= need)
    good = {k: abs(f - tgt) <= tol and enough(b.get(k) or {})
            for k, f in (("A", fa), ("B", fb))}
    upd["belt_good"] = good
    upd["done"] = good["A"] and good["B"]
    if upd["done"]:
        return "Both belts good. Done.", upd
    off = [k for k, f in (("A", fa), ("B", fb)) if abs(f - tgt) > tol]
    if not off:
        return "On target, but not from %d good plucks each: measure both again" % need, upd
    last = b.get("last_belt")
    # take turns: the belt not changed last, if it still needs it
    if len(off) == 1:
        k = off[0]
    elif last in ("A", "B"):
        k = "B" if last == "A" else "A"
    else:
        k = "A"                        # belt A first
    f = fa if k == "A" else fb
    upd["last_belt"] = k
    return "%s belt %s a little: %.1f Hz %s. Then measure both again" % (
        "Tighten" if f < tgt else "Loosen", k, abs(f - tgt),
        "low" if f < tgt else "high"), upd
