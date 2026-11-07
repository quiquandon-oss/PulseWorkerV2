"""PEV-1 pinned inputs: committed files match their manifest hashes and contain no sealed-period reading."""
import hashlib
import json
from pathlib import Path

D = Path(__file__).resolve().parent / "evaluations" / "pev1-inputs"
SEAL_START, SEAL_END = 1784678400000, 1791579600000


def test_hashes_match_manifest():
    manifest = (D / "MANIFEST.md").read_text()
    files = [D / "postseal-candles" / c / "observations.jsonl" for c in ("BTC", "ETH", "LINK")]
    files.append(D / "experiment5-sentiment-postseal.json")
    for f in files:
        assert hashlib.sha256(f.read_bytes()).hexdigest() in manifest, f


def test_no_sealed_period_data_committed():
    for c in ("BTC", "ETH", "LINK"):
        for line in (D / "postseal-candles" / c / "observations.jsonl").read_text().splitlines():
            if line.strip():
                assert json.loads(line)["available_at"] > SEAL_END
    for r in json.loads((D / "experiment5-sentiment-postseal.json").read_text())["series"]:
        assert int(r["observation_ts"]) > SEAL_END
