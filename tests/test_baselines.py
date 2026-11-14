"""Archived baselines (baselines/**/SHA256SUMS) are immutable records: every listed file must still match its
checksum and no file may be added to or removed from the archive. Reads baselines/ only; writes nothing."""
import hashlib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Baselines(unittest.TestCase):
    def test_every_archived_baseline_matches_its_checksums(self):
        for sums in sorted((ROOT / "baselines").rglob("SHA256SUMS")):
            base = sums.parent
            listed = dict(reversed(line.split("  ", 1)) for line in sums.read_text(encoding="utf-8").splitlines())
            present = {p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file() and p.name != "SHA256SUMS"}
            self.assertEqual(present, set(listed), f"{base.name}: files added or removed")
            for rel, digest in listed.items():
                self.assertEqual(hashlib.sha256((base / rel).read_bytes()).hexdigest(), digest, f"{base.name}/{rel} changed")


if __name__ == "__main__":
    unittest.main()
