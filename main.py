#!/usr/bin/env python3
"""Building Design Simulator - command line entry.

Examples:
  python main.py "Design a 10-floor residential building in Mumbai with
                  4 units per floor, budget 10 crore, parking and green roof"
  python main.py --dict inputs.json --no-pdf
  python main.py --web
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import config as cfg
from modules import pipeline


def _print_summary(summary: dict) -> None:
    req = summary["requirements"]
    print(f"\n{cfg.APP_NAME} v{cfg.VERSION}")
    print("=" * 74)
    print(f"Brief   : {req.get('raw_text') or '(form inputs)'}")
    print(f"Type    : {req['building_type']} | City: {req['city']} | "
          f"Zone {req['seismic_zone']} | {req['floors']} floors | "
          f"soil {req['soil_type']} | Vb {req['vb']} m/s")
    if req.get("warnings"):
        print(f"Notes   : {len(req['warnings'])} input warning(s)")
    print("-" * 74)
    print(f"{'ID':<3} {'Design':<28} {'Cost (Cr)':>10} {'Score':>7} "
          f"{'Checks':>9} {'Drift':>8} {'FEA':>6} {'Rank':>5}")
    for r in summary["results"]:
        a = r["analysis"]
        fea_r = a.get("fea") or {}
        fea_cell = (f"{fea_r['column_max_interaction']:.2f}"
                    if fea_r.get("ok") else "-")
        print(f"{r['design']['id']:<3} {r['design']['name']:<28} "
              f"{r['cost']['total_inr'] / 1e7:>10.2f} {r['score']:>7.0f} "
              f"{a['passed']:>2}/{a['total_checks']:<6} "
              f"{a['drift']['max_index']:>8.4f} {fea_cell:>6} {r['rank']:>5}")
    w = summary["winner"]
    print("-" * 74)
    print(f"WINNER  : {w['id']} - {w['name']} (score {w['score']:.0f}/100)")
    print("FEA     : worst OpenSees column interaction "
          "(0.00-1.00; '-' = verification skipped)")
    ga = summary.get("genetic") or {}
    if ga.get("enabled"):
        sur = ga.get("surrogate") or {}
        sur_txt = (f"R2 {sur['r2_train']} on {sur['samples']} samples"
                   if sur.get("trained") else
                   f"warming up ({sur.get('samples', 0)}/"
                   f"{sur.get('min_samples', '?')} samples)")
        print(f"GA      : genetic grid search ON - surrogate {sur_txt}")
    if req.get("warnings"):
        for msg in req["warnings"][:8]:
            print(f"  ! {msg}")
    files = summary.get("files", {})
    if "dir" in files:
        print(f"\nOutputs : {files['dir']}")
        if "pdf" in files:
            print(f"  PDF   : {files['pdf']}")
        print("  3D    : design_*_3d.html (open in a browser)")
    if summary.get("pdf_error"):
        print(f"  (PDF failed: {summary['pdf_error']})")
    print(f"\n{cfg.DISCLAIMER}\n")


def _interactive() -> str:
    print(f"{cfg.APP_NAME} v{cfg.VERSION} - enter your brief "
          "(blank line to run with defaults):")
    try:
        line = input("> ").strip()
    except EOFError:
        line = ""
    return line


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=cfg.APP_NAME)
    ap.add_argument("text", nargs="*", help="natural-language design brief")
    ap.add_argument("--dict", dest="dict_file", help="structured requirements JSON file")
    ap.add_argument("--out", help="output directory (default: output/run_<timestamp>)")
    ap.add_argument("--no-pdf", action="store_true", help="skip PDF generation")
    ap.add_argument("--no-images", action="store_true", help="skip charts/drawings")
    ap.add_argument("--json", action="store_true", help="print full summary as JSON")
    ap.add_argument("--seed", type=int, default=None,
                    help="seed for the genetic search (generation itself "
                         "is deterministic)")
    ap.add_argument("--no-genetic", action="store_true",
                    help="skip Phase-3A genetic grid search")
    ap.add_argument("--web", action="store_true", help="launch the Streamlit dashboard")
    ap.add_argument("--version", action="version", version=f"{cfg.APP_NAME} {cfg.VERSION}")
    args = ap.parse_args(argv)

    if args.web:
        import subprocess
        ui = Path(__file__).parent / "ui" / "web_app.py"
        cmd = [sys.executable, "-m", "streamlit", "run", str(ui)]
        print("Launching dashboard:", " ".join(cmd))
        return subprocess.call(cmd)

    text = " ".join(args.text).strip()
    data = None
    if args.dict_file:
        data = json.loads(Path(args.dict_file).read_text(encoding="utf-8"))
    if not text and not data:
        text = _interactive()

    try:
        summary = pipeline.run(text=text or None, data=data,
                               out_dir=args.out,
                               make_pdf=not args.no_pdf,
                               make_images=not args.no_images,
                               seed=args.seed,
                               # None (not False) keeps GENETIC_ENABLED env
                               # in control unless --no-genetic was passed
                               genetic=False if args.no_genetic else None)
    except Exception as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))
    else:
        _print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
