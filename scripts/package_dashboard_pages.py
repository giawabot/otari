"""Package the built dashboard for a Cloudflare Pages (or any static host) deploy.

Why this exists: Pages' native SPA fallback (serve index.html for unmatched
paths when no 404.html exists) proved unreliable in production — deep links
like /keys and /reset-password came back as empty 404s, which silently broke
password-reset and verification links built from ui_base_url. The _redirects
catch-all (/* /index.html 200) is NOT an option: Pages rejects it at upload
time as an infinite loop (it would strip .html/index and re-trigger itself).

The deterministic mechanism is a 404.html: Pages serves it for any unmatched
path (with a 404 status, which the SPA does not care about). It must be a
copy of the freshly built index.html — index.html references content-hashed
assets, so a checked-in static copy would rot on the next build. This script
generates it at packaging time, alongside the dashboard-build.json the
dashboard's stale-tab warning reads.

Usage (from the repo root, after `make dashboard`):
    python scripts/package_dashboard_pages.py [output.zip] [dashboard_dir]

`dashboard_dir` defaults to this tree's build output; pass the dashboard
output of a different worktree (e.g. the cross-origin dashsplit build) to
package that bundle instead.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DASHBOARD_OUT = REPO_ROOT / "src" / "gateway" / "static" / "dashboard"
DEFAULT_ZIP = "northrouter-dashboard.zip"


def gateway_version() -> str:
    """Resolve __version__ the way src/gateway/version.py does (OTARI_VERSION env, else default)."""
    return os.environ.get("OTARI_VERSION") or "0.0.0-dev"



def main() -> None:
    out_zip = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(DEFAULT_ZIP)
    dashboard_out = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_DASHBOARD_OUT
    index = dashboard_out / "index.html"
    if not index.is_file():
        raise SystemExit(f"{index} not found — run `make dashboard` first")

    with tempfile.TemporaryDirectory(prefix="pages-stage-") as tmp:
        stage = Path(tmp) / "site"
        shutil.copytree(dashboard_out, stage)

        # The SPA fallback: same bytes as the built index.html, so the hashed
        # asset references always match this exact build.
        shutil.copyfile(stage / "index.html", stage / "404.html")

        # Same algorithm as the gateway's dynamic route, so the stale-tab
        # warning keeps working against a static file.
        build_id = hashlib.sha256(index.read_bytes()).hexdigest()[:16]
        (stage / "dashboard-build.json").write_text(
            json.dumps({"build": build_id, "version": gateway_version()}), encoding="utf-8"
        )

        if out_zip.exists():
            out_zip.unlink()
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
            for path in sorted(stage.rglob("*")):
                if path.is_file():
                    zf.write(path, path.relative_to(stage))

    print(f"wrote {out_zip} (build {build_id}, {gateway_version()}) — includes 404.html SPA fallback")


if __name__ == "__main__":
    main()
