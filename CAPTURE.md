# Corridor capture — field checklist

Target: the junction RoadTwin diagnoses as worst bottleneck.
**80 Feet Road, Koramangala — 12.93592 N, 77.62807 E**

Safety first: never operate a camera while driving. Passenger captures, or walk it.

## Drop photos here

    data/reality/capture01/raw/
        pass1_forward/     ~40   drive/walk the corridor one way
        pass2_reverse/     ~40   come back the other way   <-- MOST IMPORTANT
        pass3_lateral/     ~30   different lane / offset a few metres
        pass4_sideroad/    ~30   the cross street, into the junction
        pass5_junction/    ~50   slow orbit of the junction itself

~190 diverse frames beats 1,000 near-identical ones.

## Why pass2_reverse matters most

A single forward pass never revisits anything, so error accumulates unchecked —
that is the scale drift that produced 74 m of Sim(3) error and blocked Gate 4.
Seeing the same shopfront on the way back gives COLMAP a loop-closure
constraint. It is the one pass that fixes the alignment, not just the looks.

## Camera settings

- **Lock exposure and focus** (tap-and-hold on iPhone). Varying exposure across
  frames makes photometric optimisation fight itself.
- **1-3 m spacing.** Walking pace, or burst capture from a slow vehicle.
- **Keep GPS/location on** — EXIF GPS is what scores the alignment.
- **Do not shoot while stopped at the signal.** Fifty identical frames cost
  compute and add no information.
- Landscape, avoid heavy zoom (it changes intrinsics mid-sequence).
- Overcast or even light is easier than harsh sun and hard shadows.

## When you get back

    .venv/bin/python -c "
    import sys; sys.path.insert(0,'backend')
    from pathlib import Path
    from roadtwin.reality.ingest import ingest, report
    print(report(ingest(Path('data/reality/capture01'))))"

Then the existing gates, unchanged:

    reconstruct()   -> registered frames, sparse points
    align_scene()   -> median / P95 / max error in metres
    msplat-train    -> splats

**Decide the pass/fail before looking:** the alignment clears if median error
drops below ~3 m. Anything above that and the reality layer stays a visual
asset, exactly as it is now.
