import importlib.util
from pathlib import Path
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("transfer_file", Path(__file__).resolve().parents[1] / "scripts/transfer_file.py")
transfer_file = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transfer_file)


class TransferTests(unittest.TestCase):
    def test_copy_preserves_source_and_refuses_existing_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "中文 空格.png"
            source.write_bytes(b"complete file contents")
            destination = root / "候选.png"
            result = transfer_file.transfer(source, destination)
            self.assertEqual(result["bytes"], len(source.read_bytes()))
            self.assertEqual(source.read_bytes(), destination.read_bytes())
            source.write_bytes(b"changed")
            with self.assertRaises(FileExistsError):
                transfer_file.transfer(source, destination)
            self.assertEqual(destination.read_bytes(), b"complete file contents")

    def test_symlinks_and_non_files_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "image.png"
            source.write_bytes(b"image")
            alias = root / "alias"
            alias.symlink_to(source)
            with self.assertRaises(OSError):
                transfer_file.transfer(alias, root / "output")
            with self.assertRaises((OSError, ValueError)):
                transfer_file.transfer(root, root / "output")
            alias.unlink()
            alias.symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                transfer_file.transfer(source, alias / "output")
            self.assertFalse((root / "output").exists())


if __name__ == "__main__":
    unittest.main()
