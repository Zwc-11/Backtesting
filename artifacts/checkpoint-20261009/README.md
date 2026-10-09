# Saved project checkpoint

This directory preserves the complete October 9 data snapshot, built packages,
validation evidence and dashboard screenshots alongside the source repository.
The compressed backup is split into four parts below GitHub's individual-file
limit. Reassembling the parts produces the original verified archive; the
adjacent manifest also verifies each of its 1,011 files on restore.

From the repository root:

```bash
(cd artifacts/checkpoint-20261009 && sha256sum --check SHA256SUMS)
cat artifacts/checkpoint-20261009/xasset-20261009-final.tar.gz.part-* \
  > /tmp/xasset-20261009-final.tar.gz
cp artifacts/checkpoint-20261009/xasset-20261009-final.tar.gz.manifest.json /tmp/
uv sync --locked
uv run --frozen python scripts/backup.py restore /tmp/xasset-20261009-final.tar.gz data
```

Restore refuses an existing destination. The archive contains market observations,
raw source receipts, audit reports, locked research registries and input snapshots,
the relationship map, live captures and the archived research implementation.
It contains no credentials or dependency caches. The built wheel/source archive
are provided separately; installing the package does not install the data.

The 570 stable map edges are descriptive, and no strategy is approved to trade.
See [the current roadmap](../../docs/ROADMAP.md) and
[operations guide](../../docs/OPERATIONS.md) for remaining work and deployment.
