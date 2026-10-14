"""The minecraftdanmark.dk block frame: UTM zone 32N (EPSG:25832) with
X = E - 600000 and Z = 6200000 - N, one block per metre at scale 1.

The Krüger series are those of src/projection/utm.rs (Karney 2011), so the
planner and the generator agree on every block without pyproj.
"""
import math

_F = 1 / 298.257222101
_N = _F / (2 - _F)
_P = [_N ** k for k in range(1, 7)]
_K0A = 0.9996 * 6378137.0 / (1 + _N) * (1 + _P[1] / 4 + _P[3] / 64 + _P[5] / 256)
_E = math.sqrt(_F * (2 - _F))
_ALPHA = [
    _P[0] / 2 - 2 * _P[1] / 3 + 5 * _P[2] / 16 + 41 * _P[3] / 180 - 127 * _P[4] / 288
    + 7891 * _P[5] / 37800,
    13 * _P[1] / 48 - 3 * _P[2] / 5 + 557 * _P[3] / 1440 + 281 * _P[4] / 630
    - 1983433 * _P[5] / 1935360,
    61 * _P[2] / 240 - 103 * _P[3] / 140 + 15061 * _P[4] / 26880 + 167603 * _P[5] / 181440,
    49561 * _P[3] / 161280 - 179 * _P[4] / 168 + 6601661 * _P[5] / 7257600,
    34729 * _P[4] / 80640 - 3418889 * _P[5] / 1995840,
    212378941 * _P[5] / 319334400,
]
_BETA = [
    _P[0] / 2 - 2 * _P[1] / 3 + 37 * _P[2] / 96 - _P[3] / 360 - 81 * _P[4] / 512
    + 96199 * _P[5] / 604800,
    _P[1] / 48 + _P[2] / 15 - 437 * _P[3] / 1440 + 46 * _P[4] / 105 - 1118711 * _P[5] / 3870720,
    17 * _P[2] / 480 - 37 * _P[3] / 840 - 209 * _P[4] / 4480 + 5569 * _P[5] / 90720,
    4397 * _P[3] / 161280 - 11 * _P[4] / 504 - 830251 * _P[5] / 7257600,
    4583 * _P[4] / 161280 - 108847 * _P[5] / 3991680,
    20648693 * _P[5] / 638668800,
]
LON0 = 9.0
EAST_OFFSET = 600000.0
NORTH_OFFSET = 6200000.0
CHUNK = 16
REGION = 512
# Points per edge when tracing a rectangle's outline; see src/projection/mod.rs.
EDGE_SAMPLES = 128
OUTLINE_MARGIN_DEG = 1e-7


def _conformal_tan(tau):
    sigma = math.sinh(_E * math.atanh(_E * tau / math.hypot(tau, 1)))
    return tau * math.hypot(sigma, 1) - sigma * math.hypot(tau, 1)


def utm32(lat, lon):
    """Easting and northing in metres."""
    lam = math.radians(lon - LON0)
    tau_p = _conformal_tan(math.tan(math.radians(lat)))
    xi_p = math.atan2(tau_p, math.cos(lam))
    eta_p = math.asinh(math.sin(lam) / math.hypot(tau_p, math.cos(lam)))
    xi, eta = xi_p, eta_p
    for j, a in enumerate(_ALPHA, 1):
        xi += a * math.sin(2 * j * xi_p) * math.cosh(2 * j * eta_p)
        eta += a * math.cos(2 * j * xi_p) * math.sinh(2 * j * eta_p)
    return 500000 + _K0A * eta, _K0A * xi


def utm32_inverse(easting, northing):
    """Latitude and longitude in degrees."""
    xi, eta = northing / _K0A, (easting - 500000) / _K0A
    xi_p, eta_p = xi, eta
    for j, b in enumerate(_BETA, 1):
        xi_p -= b * math.sin(2 * j * xi) * math.cosh(2 * j * eta)
        eta_p -= b * math.cos(2 * j * xi) * math.sinh(2 * j * eta)
    sinh_eta = math.sinh(eta_p)
    tau_p = math.sin(xi_p) / math.hypot(sinh_eta, math.cos(xi_p))
    lam = math.atan2(sinh_eta, math.cos(xi_p))
    e2, tau = _E * _E, tau_p
    for _ in range(8):
        tau_i = _conformal_tan(tau)
        step = ((tau_p - tau_i) / math.hypot(tau_i, 1) * (1 + (1 - e2) * tau * tau)
                / ((1 - e2) * math.hypot(tau, 1)))
        tau += step
        if abs(step) < 1e-14:
            break
    return math.degrees(math.atan(tau)), LON0 + math.degrees(lam)


def block(lat, lon, scale=1.0):
    """Fractional block (x, z); block X spans [X, X + 1) and north is -Z."""
    e, n = utm32(lat, lon)
    return (e - EAST_OFFSET) * scale, (NORTH_OFFSET - n) * scale


def latlon(x, z, scale=1.0):
    return utm32_inverse(x / scale + EAST_OFFSET, NORTH_OFFSET - z / scale)


def rect_bbox(rect, scale, margin=OUTLINE_MARGIN_DEG):
    """South, west, north, east around the inclusive block rectangle
    [min_x, min_z, max_x, max_z], outer block edges included."""
    x0, z0, x1, z1 = rect[0], rect[1], rect[2] + 1, rect[3] + 1
    lats, lons = [], []
    for i in range(EDGE_SAMPLES + 1):
        t = i / EDGE_SAMPLES
        x, z = x0 + (x1 - x0) * t, z0 + (z1 - z0) * t
        for bx, bz in ((x0, z), (x1, z), (x, z0), (x, z1)):
            lat, lon = latlon(bx, bz, scale)
            lats.append(lat)
            lons.append(lon)
    return [min(lats) - margin, min(lons) - margin, max(lats) + margin, max(lons) + margin]


def bbox_rect(bounds, scale):
    """The chunk-aligned block rectangle covering a south, west, north, east bbox."""
    s, w, n, e = bounds
    xs, zs = [], []
    for i in range(EDGE_SAMPLES + 1):
        t = i / EDGE_SAMPLES
        lat, lon = s + (n - s) * t, w + (e - w) * t
        for la, lo in ((lat, w), (lat, e), (s, lon), (n, lon)):
            x, z = block(la, lo, scale)
            xs.append(x)
            zs.append(z)
    floor = lambda v: math.floor(v / CHUNK) * CHUNK
    ceil = lambda v: math.ceil(v / CHUNK) * CHUNK
    return [floor(min(xs)), floor(min(zs)), ceil(max(xs)) - 1, ceil(max(zs)) - 1]


def cells(bounds, scale, regions):
    """Squares of `regions` x `regions` region files on the MCDK grid, clipped to
    the area, north to south and west to east. Neighbours share an edge exactly."""
    size = regions * REGION
    x0, z0, x1, z1 = bbox_rect(bounds, scale)
    out = []
    for cz in range(z0 // size, z1 // size + 1):
        for cx in range(x0 // size, x1 // size + 1):
            out.append([max(x0, cx * size), max(z0, cz * size),
                        min(x1, (cx + 1) * size - 1), min(z1, (cz + 1) * size - 1)])
    return out
