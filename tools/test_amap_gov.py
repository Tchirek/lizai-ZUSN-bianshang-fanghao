import json
import os
import tempfile
import unittest
from unittest.mock import patch

import amap_gov


class AreasTests(unittest.TestCase):
    def test_empty_later_page_splits_without_losing_earlier_results(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(amap_gov, "OUT", folder), \
                patch.object(amap_gov, "EXTENT", (0, 0, 0.5, 0.5)):
            areas = amap_gov.Areas(0.5)
            cell = areas.next()["cell"]
            pois = [dict(id=str(i)) for i in range(50)]
            areas.result(dict(cell=cell, page=1, count=180, pois=pois))
            areas.result(dict(cell=cell, page=2, count=180, pois=pois))
            areas.result(dict(cell=cell, page=3, count=0, pois=[]))

            with open(os.path.join(folder, cell + ".json")) as f:
                saved = json.load(f)
            self.assertTrue(saved["split"])
            self.assertEqual(len(saved["pois"]), 100)
            self.assertEqual(len(areas.todo), 4)
            self.assertEqual(len(amap_gov.Areas(0.5).todo), 4)

    def test_existing_truncated_cell_is_resumed_as_quarters(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(amap_gov, "OUT", folder), \
                patch.object(amap_gov, "EXTENT", (0, 0, 0.5, 0.5)):
            path = os.path.join(folder, "0.0000_0.0000_0.5000.json")
            with open(path, "w") as f:
                json.dump(dict(count=230, pois=[dict(id=str(i)) for i in range(200)]), f)
            areas = amap_gov.Areas(0.5)
            self.assertEqual(len(areas.todo), 4)
            with open(path) as f:
                saved = json.load(f)
            self.assertTrue(saved["split"])
            self.assertEqual(len(saved["pois"]), 200)

    def test_short_nonempty_last_page_splits(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(amap_gov, "OUT", folder), \
                patch.object(amap_gov, "EXTENT", (0, 0, 0.5, 0.5)):
            areas = amap_gov.Areas(0.5)
            cell = areas.next()["cell"]
            for page, count in ((1, 50), (2, 50), (3, 50), (4, 20)):
                areas.result(dict(cell=cell, page=page, count=180,
                                  pois=[dict(id=f"{page}-{i}") for i in range(count)]))
            with open(os.path.join(folder, cell + ".json")) as f:
                saved = json.load(f)
            self.assertTrue(saved["split"])
            self.assertEqual(len(saved["pois"]), 170)
            self.assertEqual(len(areas.todo), 4)


if __name__ == "__main__":
    unittest.main()
