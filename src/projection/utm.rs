//! Transverse Mercator for UTM zone 32N on GRS80 (EPSG:25832), and the block
//! fixed block frame built on it.
//!
//! The series are Krüger's to sixth order in n, as given by Karney (2011),
//! "Transverse Mercator with an accuracy of a few nanometers". Within a few
//! thousand kilometres of the central meridian they agree with PROJ to well
//! under a millimetre, which covers all of Denmark (Bornholm included, even
//! though it lies in zone 33).

/// GRS80 semi-major axis in metres.
const GRS80_A: f64 = 6_378_137.0;
/// GRS80 flattening.
const GRS80_F: f64 = 1.0 / 298.257_222_101;

/// MCDK `X = E - 600000`.
pub const MCDK_EAST_OFFSET: f64 = 600_000.0;
/// MCDK `Z = 6200000 - N`.
pub const MCDK_NORTH_OFFSET: f64 = 6_200_000.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct TransverseMercator {
    /// Central meridian in degrees.
    lon0: f64,
    false_easting: f64,
    /// Rectifying radius times `k0`.
    k0_a: f64,
    e: f64,
    alpha: [f64; 6],
    beta: [f64; 6],
}

impl TransverseMercator {
    pub fn new(a: f64, f: f64, lon0: f64, k0: f64, false_easting: f64) -> Self {
        let n = f / (2.0 - f);
        let (n2, n3) = (n * n, n * n * n);
        let (n4, n5, n6) = (n3 * n, n3 * n2, n3 * n3);
        let rectifying = a / (1.0 + n) * (1.0 + n2 / 4.0 + n4 / 64.0 + n6 / 256.0);
        let alpha = [
            n / 2.0 - 2.0 * n2 / 3.0 + 5.0 * n3 / 16.0 + 41.0 * n4 / 180.0 - 127.0 * n5 / 288.0
                + 7891.0 * n6 / 37800.0,
            13.0 * n2 / 48.0 - 3.0 * n3 / 5.0 + 557.0 * n4 / 1440.0 + 281.0 * n5 / 630.0
                - 1_983_433.0 * n6 / 1_935_360.0,
            61.0 * n3 / 240.0 - 103.0 * n4 / 140.0
                + 15061.0 * n5 / 26880.0
                + 167_603.0 * n6 / 181_440.0,
            49561.0 * n4 / 161_280.0 - 179.0 * n5 / 168.0 + 6_601_661.0 * n6 / 7_257_600.0,
            34729.0 * n5 / 80640.0 - 3_418_889.0 * n6 / 1_995_840.0,
            212_378_941.0 * n6 / 319_334_400.0,
        ];
        let beta = [
            n / 2.0 - 2.0 * n2 / 3.0 + 37.0 * n3 / 96.0 - n4 / 360.0 - 81.0 * n5 / 512.0
                + 96199.0 * n6 / 604_800.0,
            n2 / 48.0 + n3 / 15.0 - 437.0 * n4 / 1440.0 + 46.0 * n5 / 105.0
                - 1_118_711.0 * n6 / 3_870_720.0,
            17.0 * n3 / 480.0 - 37.0 * n4 / 840.0 - 209.0 * n5 / 4480.0 + 5569.0 * n6 / 90720.0,
            4397.0 * n4 / 161_280.0 - 11.0 * n5 / 504.0 - 830_251.0 * n6 / 7_257_600.0,
            4583.0 * n5 / 161_280.0 - 108_847.0 * n6 / 3_991_680.0,
            20_648_693.0 * n6 / 638_668_800.0,
        ];
        Self {
            lon0,
            false_easting,
            k0_a: k0 * rectifying,
            e: (f * (2.0 - f)).sqrt(),
            alpha,
            beta,
        }
    }

    /// UTM zone 32N on ETRS89 / GRS80.
    pub fn utm32() -> Self {
        Self::new(GRS80_A, GRS80_F, 9.0, 0.9996, 500_000.0)
    }

    /// Conformal latitude's tangent for a geodetic latitude's tangent.
    fn conformal_tan(&self, tau: f64) -> f64 {
        let sigma = (self.e * (self.e * tau / tau.hypot(1.0)).atanh()).sinh();
        tau * sigma.hypot(1.0) - sigma * tau.hypot(1.0)
    }

    /// Easting and northing in metres.
    pub fn forward(&self, lat: f64, lon: f64) -> (f64, f64) {
        let lambda = (lon - self.lon0).to_radians();
        let tau_p = self.conformal_tan(lat.to_radians().tan());
        let (sin_l, cos_l) = lambda.sin_cos();
        let xi_p = tau_p.atan2(cos_l);
        let eta_p = (sin_l / tau_p.hypot(cos_l)).asinh();
        let (mut xi, mut eta) = (xi_p, eta_p);
        for (j, a) in self.alpha.iter().enumerate() {
            let k = 2.0 * (j + 1) as f64;
            xi += a * (k * xi_p).sin() * (k * eta_p).cosh();
            eta += a * (k * xi_p).cos() * (k * eta_p).sinh();
        }
        (self.false_easting + self.k0_a * eta, self.k0_a * xi)
    }

    /// Latitude and longitude in degrees.
    pub fn inverse(&self, easting: f64, northing: f64) -> (f64, f64) {
        let xi = northing / self.k0_a;
        let eta = (easting - self.false_easting) / self.k0_a;
        let (mut xi_p, mut eta_p) = (xi, eta);
        for (j, b) in self.beta.iter().enumerate() {
            let k = 2.0 * (j + 1) as f64;
            xi_p -= b * (k * xi).sin() * (k * eta).cosh();
            eta_p -= b * (k * xi).cos() * (k * eta).sinh();
        }
        let sinh_eta = eta_p.sinh();
        let (sin_xi, cos_xi) = xi_p.sin_cos();
        let tau_p = sin_xi / sinh_eta.hypot(cos_xi);
        let lambda = sinh_eta.atan2(cos_xi);
        // Newton's method on the conformal latitude, as in Karney (2011).
        let e2 = self.e * self.e;
        let mut tau = tau_p;
        for _ in 0..8 {
            let tau_i = self.conformal_tan(tau);
            let step = (tau_p - tau_i) / tau_i.hypot(1.0) * (1.0 + (1.0 - e2) * tau * tau)
                / ((1.0 - e2) * tau.hypot(1.0));
            tau += step;
            if step.abs() < 1e-14 {
                break;
            }
        }
        (tau.atan().to_degrees(), self.lon0 + lambda.to_degrees())
    }
}

/// A fixed block frame on UTM zone 32N: `X = E - 600000` and
/// `Z = 6200000 - N`, one block per metre at scale 1, so `(0, 0)` lies near
/// Samsø and north is -Z. Other scales multiply both axes.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct McdkProjection {
    pub scale: f64,
    tm: TransverseMercator,
}

impl McdkProjection {
    pub fn new(scale: f64) -> Self {
        Self {
            scale,
            tm: TransverseMercator::utm32(),
        }
    }

    /// Block `(x, z)` as fractions; block `X` spans `[X, X + 1)`.
    pub fn forward(&self, lat: f64, lon: f64) -> (f64, f64) {
        let (e, n) = self.tm.forward(lat, lon);
        (
            (e - MCDK_EAST_OFFSET) * self.scale,
            (MCDK_NORTH_OFFSET - n) * self.scale,
        )
    }

    pub fn inverse(&self, x: f64, z: f64) -> (f64, f64) {
        self.tm.inverse(
            x / self.scale + MCDK_EAST_OFFSET,
            MCDK_NORTH_OFFSET - z / self.scale,
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// (lat, lon, easting, northing) from pyproj 3.8 (EPSG:4258 -> EPSG:25832).
    const PYPROJ: [(f64, f64, f64, f64); 7] = [
        (55.8333, 10.6, 600212.8016142369, 6188684.441020456),
        (55.4036, 11.354, 649051.7636999544, 6142226.177588887),
        (55.6761, 12.5683, 724351.928637642, 6175804.02212861),
        (57.7209, 10.5839, 594345.2578742233, 6398740.409466657),
        (55.1009, 14.7066, 863917.8358138121, 6120903.692376184),
        (54.5747, 11.9283, 689268.1836778162, 6051408.782099896),
        (55.4765, 8.4594, 465829.81830068486, 6147950.765518288),
    ];

    #[test]
    fn matches_pyproj_to_a_tenth_of_a_millimetre() {
        let tm = TransverseMercator::utm32();
        for (lat, lon, e, n) in PYPROJ {
            let (pe, pn) = tm.forward(lat, lon);
            assert!((pe - e).abs() < 1e-4, "{lat},{lon}: E {pe} vs {e}");
            assert!((pn - n).abs() < 1e-4, "{lat},{lon}: N {pn} vs {n}");
        }
    }

    #[test]
    fn inverse_undoes_forward() {
        let tm = TransverseMercator::utm32();
        for (lat, lon, e, n) in PYPROJ {
            let (la, lo) = tm.inverse(e, n);
            // 1e-9 degrees is about 0.1 mm.
            assert!((la - lat).abs() < 1e-9, "{la} vs {lat}");
            assert!((lo - lon).abs() < 1e-9, "{lo} vs {lon}");
        }
    }

    #[test]
    fn the_origin_lies_near_samsoe() {
        let p = McdkProjection::new(1.0);
        let (lat, lon) = p.inverse(0.0, 0.0);
        // pyproj: 55.93497611973894, 10.600782059467294
        assert!((lat - 55.934_976_119_7).abs() < 1e-9, "{lat}");
        assert!((lon - 10.600_782_059_5).abs() < 1e-9, "{lon}");
        // Slagelse centre: E 649051.76, N 6142226.18.
        let (x, z) = p.forward(55.4036, 11.354);
        assert_eq!((x.floor(), z.floor()), (49051.0, 57773.0));
        // Skagen lies north (-Z) and a little west of the origin.
        let (x, z) = p.forward(57.7209, 10.5839);
        assert_eq!((x.floor(), z.floor()), (-5655.0, -198741.0));
    }

    #[test]
    fn scale_multiplies_both_axes() {
        let one = McdkProjection::new(1.0);
        let two = McdkProjection::new(2.0);
        let (x1, z1) = one.forward(55.4, 11.35);
        let (x2, z2) = two.forward(55.4, 11.35);
        assert!((x2 - 2.0 * x1).abs() < 1e-6 && (z2 - 2.0 * z1).abs() < 1e-6);
        let (lat, lon) = two.inverse(x2, z2);
        assert!((lat - 55.4).abs() < 1e-10 && (lon - 11.35).abs() < 1e-10);
    }
}
