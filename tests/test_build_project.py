import io
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_project import safe_unpack, find_project


class BuildToolsTest(unittest.TestCase):
    def test_unpack_good_zip(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            with zipfile.ZipFile(p / 'src.zip', 'w') as z:
                z.writestr('app/hello.txt', 'hello')
            target = p / 'unpacked'
            target.mkdir()
            safe_unpack(p / 'src.zip', target)
            self.assertEqual((target / 'app/hello.txt').read_text(), 'hello')

    def test_reject_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            with zipfile.ZipFile(p / 'src.zip', 'w') as z:
                z.writestr('../../oops.txt', 'danger')
            target = p / 'unpacked'
            target.mkdir()
            with self.assertRaises(ValueError):
                safe_unpack(p / 'src.zip', target)

    def test_find_nested_xcode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'src' / 'Demo.xcodeproj').mkdir(parents=True)
            self.assertEqual(find_project(root, '*.xcodeproj').name, 'Demo.xcodeproj')


if __name__ == '__main__':
    unittest.main()
