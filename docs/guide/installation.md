# Installation

## Standard — from PyPI

niwaki targets Python 3.12+ and installs like any package:

```bash
uv add niwaki          # or: pip install niwaki
```

## Restricted networks — the offline wheelhouse

Many enterprise environments cannot reach PyPI (firewall policy, air-gapped
management networks).  For them, **every GitHub Release ships an offline
wheelhouse**: a single zip containing the niwaki wheel *and every
dependency*, as prebuilt wheels, for the mainstream platforms
(Linux x86-64, Windows amd64, macOS arm64) and CPython 3.12 / 3.13.

Step by step:

1. **On a machine with internet access**, open the
   [releases page](https://github.com/k3l0-dev/niwaki/releases) and download
   two assets from the latest release:
   - `niwaki-<version>-offline-wheelhouse.zip`
   - `SHA256SUMS.txt`

2. **Verify the download** (same command on Linux/macOS; on Windows use
   `CertUtil -hashfile <file> SHA256` and compare by eye):

   ```bash
   grep offline-wheelhouse SHA256SUMS.txt | sha256sum -c -
   ```

   The release assets also carry GitHub build-provenance attestations —
   where the `gh` CLI is available, `gh attestation verify <file>
   --repo k3l0-dev/niwaki` proves the zip was built by this repository's
   release workflow.

3. **Transfer the zip** to the restricted machine through your approved
   channel (file gateway, USB policy, artifact store…).

4. **Install without any index**:

   ```bash
   unzip niwaki-<version>-offline-wheelhouse.zip -d wheelhouse
   pip install --no-index --find-links=wheelhouse niwaki
   ```

   The `uv` equivalent:

   ```bash
   uv pip install --no-index --find-links=wheelhouse niwaki
   ```

5. **Check the result**:

   ```bash
   python -c "import niwaki; print(niwaki.__version__)"
   ```

### Notes for platform teams

- **Internal mirror (recommended long-term)** — if your organisation runs
  Artifactory, Nexus or devpi, upload the wheelhouse contents once and
  install with `pip install --index-url <internal-index> niwaki`; the
  wheelhouse is exactly the set of packages to mirror.
- **Other platforms / Pythons** — the wheelhouse covers the mainstream
  combinations.  Anything else installs from the `niwaki-<version>.tar.gz`
  sdist (niwaki itself is pure Python); only the dependencies then need a
  matching wheel source.
- **Proxy rather than air gap** — if pip is merely behind a proxy,
  `pip install --proxy http://proxy:8080 niwaki` may be all you need; the
  wheelhouse is for when no index is reachable at all.

## Working on the SDK itself

```bash
git clone https://github.com/k3l0-dev/niwaki
cd niwaki
uv sync --extra dev
```

The documentation site is built locally with `bash scripts/docs.sh open`.
