import contextlib
import io
import json
import math
import unittest

import numpy as np

import skraafoto_facades as sf

# A 2025 skråfoto camera looking north over Nyhavn, as the STAC item carries it.
NORTH = {
    'id': 'north', 'assets': {'data': {'href': 'unused'}},
    'properties': {
        'direction': 'north', 'datetime': '2025-04-27T09:32:43Z',
        'pers:interior_orientation': {'focal_length': 123.38, 'pixel_spacing': [0.00376, 0.00376],
                                      'sensor_array_dimensions': [14144.0, 10560.0],
                                      'principal_point_offset': [0.0, 0.0]},
        'pers:perspective_center': [725654.34, 6173983.97, 2202.03],
        'pers:rotation_matrix': [0.9999920401802164, -0.0036980793149057507, -0.0014979270973634626,
                                 0.0036753016326038957, 0.7076321593776156, 0.7065714536920393,
                                 -0.0015529758909038672, -0.7065713348445103, 0.7076401183100975],
    },
}


class TileNodeIds(unittest.TestCase):
    def test_aot1_rounds_half_away_from_zero_at_one_micro_degree(self):
        # 55.6798845 is half way between two micro-degrees; the archive keeps ...885.
        aot1, aot2 = sf.tile_node_ids(55.6798845, 12.5906)
        lat_e7, lon_e7 = 556798850, 125906000
        self.assertEqual(aot1, (1 << 61) | ((lat_e7 + 900_000_000) << 30) | lon_e7)
        self.assertEqual(aot2, (1 << 61) | ((556798845 + 900_000_000) << 30) | lon_e7)

    def test_negative_longitudes_wrap_like_rem_euclid(self):
        # -1e-7 degrees: a tenth of a micro-degree rounds to 0 in AOT1, and wraps to the
        # top of the 30-bit longitude field in AOT2.
        aot1, aot2 = sf.tile_node_ids(0.0, -0.0000001)
        self.assertEqual(aot1 & ((1 << 30) - 1), 0)
        self.assertEqual(aot2 & ((1 << 30) - 1), (1 << 30) - 1)


def square(owner, x, y, w, h, first_node):
    """A building as buildings_from_osm returns it, and its node positions (UTM)."""
    ids = list(range(first_node, first_node + 4))
    pos = dict(zip(ids, [(x, y), (x + w, y), (x + w, y + h), (x, y + h)]))
    return (owner, [ids + ids[:1]]), pos


class Blocks(unittest.TestCase):
    def setUp(self):
        self.utm = {}
        self.base = []
        for building, pos in [square(('way', 1), 0, 0, 30, 10, 100), square(('way', 9), 40, 0, 10, 10, 110)]:
            self.base.append(building)
            self.utm.update(pos)
        self.extra, self.extra_utm = [], {}
        for building, pos in [square(('way', 2), 0, 0, 10, 10, 200), square(('way', 3), 10, 0, 10, 10, 210),
                              square(('way', 4), 20, 0, 10, 10, 220), square(('way', 5), 40, 0, 10, 10, 230),
                              square(('way', 6), 60, 0, 10, 10, 240)]:
            self.extra.append(building)
            self.extra_utm.update(pos)

    def owners(self, buildings):
        return sorted(owner[1] for owner, _ in buildings)

    def test_houses_of_a_block_are_sampled_beside_the_free_ones(self):
        # 2-4 make up block 1; 5 duplicates building 9 alone, so 9 is no block; 6 is free.
        self.assertEqual(self.owners(sf.in_blocks(self.extra, self.extra_utm, self.base, self.utm)), [2, 3, 4])
        self.assertEqual(self.owners(sf.without_overlaps(self.extra, self.extra_utm, self.base, self.utm)), [6])

    def test_a_house_with_its_garage_and_shed_is_no_block(self):
        outbuildings = {('way', 3), ('way', 4)}
        self.assertEqual(sf.in_blocks(self.extra, self.extra_utm, self.base, self.utm, outbuildings), [])


class WallEdges(unittest.TestCase):
    def test_each_id_scheme_names_the_wall_once(self):
        aliases = {1: [11, 21], 2: [12, 21 + 1]}
        edges = sf.wall_edges(1, 2, 7.5, aliases)
        self.assertEqual([(e['node_a'], e['node_b']) for e in edges], [(1, 2), (11, 12), (21, 22)])
        self.assertTrue(all(e['s0'] == 0.0 and e['s1'] == 7.5 for e in edges))

    def test_schemes_that_agree_are_not_repeated(self):
        edges = sf.wall_edges(1, 2, 3.0, {1: [11, 11], 2: [12, 12]})
        self.assertEqual(len(edges), 2)


class Camera(unittest.TestCase):
    def test_a_point_projects_into_the_middle_of_its_photo(self):
        photo = sf.Photo(NORTH)
        col, row = photo.project(np.array([725731.7]), np.array([6176293.5]), np.array([12.0]))
        self.assertAlmostEqual(float(col[0]), 7815.6, delta=1.0)
        self.assertAlmostEqual(float(row[0]), 4381.0, delta=1.0)

    def test_the_side_cameras_principal_point_offset_is_added(self):
        side = json.loads(json.dumps(NORTH))
        side['properties']['pers:interior_orientation']['principal_point_offset'] = [0.0, 6.68]
        a = sf.Photo(NORTH).project(np.array([725731.7]), np.array([6176293.5]), np.array([12.0]))
        b = sf.Photo(side).project(np.array([725731.7]), np.array([6176293.5]), np.array([12.0]))
        self.assertAlmostEqual(float(a[1][0] - b[1][0]), 6.68 / 0.00376, places=3)


class SkippedPhotos(unittest.TestCase):
    """A photo the service will not serve is skipped for the rest of the run, not its building."""

    def setUp(self):
        sf._SKIPPED.clear()
        self.opened = []
        self.real_open = sf.open_photo

        def open_photo(url):
            self.opened.append(url)
            raise OSError('HTTP response code: 404')
        sf.open_photo = open_photo

    def tearDown(self):
        sf.open_photo = self.real_open
        sf._SKIPPED.clear()

    def photo(self, pid):
        item = json.loads(json.dumps(NORTH))
        item['id'] = pid
        item['assets']['data']['href'] = f'https://example.invalid/{pid}.tif'
        return sf.Photo(item)

    def test_a_photo_that_fails_gives_no_view_and_is_not_tried_again(self):
        photo = self.photo('missing')
        bounds = (725725.0, 6176287.0, 725738.0, 6176300.0)
        log = io.StringIO()
        with contextlib.redirect_stdout(log):
            self.assertIsNone(photo.view(bounds, 0.0, 20.0))
            self.assertIsNone(photo.view(bounds, 0.0, 20.0), 'the next building does not wait on it')
        self.assertEqual(len(self.opened), 1)
        self.assertIn('photo missing skipped for the rest of this run: OSError: HTTP response code: 404', log.getvalue())
        self.assertEqual(log.getvalue().count('skipped'), 1, 'logged once')
        self.assertEqual(sf.skipped_photos(), {'missing': 'OSError: HTTP response code: 404'})

    def test_the_index_passes_a_skipped_photo_over_for_the_next_best(self):
        index = sf.PhotoIndex.__new__(sf.PhotoIndex)
        bad, good = self.photo('bad'), self.photo('good')
        index.by_direction = {'north': [bad, good]}
        self.assertIs(index.best('north', 725731.7, 6176293.5, 12.0), bad)
        with contextlib.redirect_stdout(io.StringIO()):
            sf.skip_photo(bad, OSError('HTTP response code: 404'))
        self.assertIs(index.best('north', 725731.7, 6176293.5, 12.0), good)


class Colour(unittest.TestCase):
    def test_oklab_round_trip(self):
        rgb = np.array([[200, 170, 90], [128, 128, 128], [255, 255, 255], [150, 60, 40]], dtype=np.float32)
        np.testing.assert_allclose(sf.srgb_from_oklab(sf.oklab_from_srgb(rgb)), rgb, atol=0.5)

    def test_chroma_restore_keeps_greys_grey(self):
        grey = sf.restore_chroma(np.array([[150, 150, 150]], dtype=np.float32))[0]
        self.assertLess(float(np.ptp(grey)), 1.5)
        ochre = sf.restore_chroma(np.array([[188, 154, 129]], dtype=np.float32))[0]
        self.assertGreater(float(ochre[0] - ochre[2]), 188 - 129)

    def test_shadow_gain_is_the_ratio_of_sunlit_to_shaded_medians(self):
        sun = np.full((18000, 3), 200.0, dtype=np.float32)
        shade = np.full((18000, 3), 100.0, dtype=np.float32)
        tex = np.concatenate([sun, shade])
        lit = np.r_[np.ones(18000, bool), np.zeros(18000, bool)]
        gain, n_sun, n_shade = sf.shadow_gain([{'tex': tex, 'lit': lit}])
        expected = sf.to_linear(np.array([200.0]))[0] / sf.to_linear(np.array([100.0]))[0]
        np.testing.assert_allclose(gain, [expected] * 3, rtol=1e-5)
        self.assertEqual((n_sun, n_shade), (6000, 6000))

    def test_too_few_pixels_on_either_side_leave_the_colours_alone(self):
        tex = np.full((300, 3), 120.0, dtype=np.float32)
        gain, _, _ = sf.shadow_gain([{'tex': tex, 'lit': np.r_[np.ones(150, bool), np.zeros(150, bool)]}])
        np.testing.assert_array_equal(gain, [1.0, 1.0, 1.0])

    def test_no_walls_at_all_leave_the_colours_alone(self):
        gain, n_sun, n_shade = sf.shadow_gain([])
        np.testing.assert_array_equal(gain, [1.0, 1.0, 1.0])
        self.assertEqual((n_sun, n_shade), (0, 0))


class Sun(unittest.TestCase):
    def test_late_morning_in_copenhagen_in_april(self):
        s = sf.sun_direction('2025-04-27T09:32:43Z', 55.68, 12.59)
        azimuth = math.degrees(math.atan2(s[0], s[1])) % 360
        elevation = math.degrees(math.asin(s[2]))
        self.assertAlmostEqual(azimuth, 147, delta=2)
        self.assertAlmostEqual(elevation, 44, delta=2)


if __name__ == '__main__':
    unittest.main()
