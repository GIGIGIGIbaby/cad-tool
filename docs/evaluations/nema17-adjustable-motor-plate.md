# V3 Evaluation 001: Adjustable NEMA 17 Motor Plate

## Objective

Create a manufacturable 2D plate-style part for mounting a NEMA 17 stepper motor to a belt-driven linear axis. The plate moves relative to the frame through two elongated M6 mounting slots, permitting belt-tension adjustment along X. This exercise assesses native FreeCAD PartDesign / Sketcher use, model observation, controlled parameter revision, Git snapshots, and recovery. It does not assess strength, tolerance selection, actual fastener procurement, motor fit beyond the stated nominal geometry, CAM, FEM, or GUI interaction.

## Coordinate convention

- `XY` is the plate plane; `Z` is thickness.
- Plate centre, pilot bore centre, and motor pattern centre are `(0, 0)`.
- All dimensions are millimetres.
- Sharp external corners and all hole/slot edges are intentional: do not add fillets or chamfers; the analytical acceptance volume assumes sharp edges.
- `SlotLength` means the capsule's total outside-to-outside length along X. Its straight middle length is `SlotLength - SlotWidth`; it is not travel distance or centre-to-centre length.

## Required native model structure

Use a `Spreadsheet::Sheet` named `Params` and a `PartDesign::Body` whose **internal Name** is `MotorPlate` (Label may differ). Dimensional source values must be named aliases in `Params`; use expressions to connect source parameters to sketch constraints and feature properties.

The Body must contain this semantic feature chain (labels can differ; acceptance checks typed Profile/Tip links rather than labels):

```text
BaseSketch -> BasePad -> MotorSketch -> MotorPocket -> SlotSketch -> SlotPocket
```

- `BaseSketch` is a constrained 100 x 80 rectangle centred at the origin.
- `BasePad` has thickness 8.
- `MotorSketch` contains the central pilot bore and four motor clearances; `MotorPocket` cuts them through all.
- `SlotSketch` contains the two full capsule profiles; `SlotPocket` cuts them through all.
- Do not use a frozen `Part::Feature.Shape = ...` as a substitute for the native PartDesign chain.
- Set `cad_outputs = ["MotorPlate"]` or the corresponding actual Body object; this refers to the internal Name, not merely a Label.
- The final Body Tip must be the last Pocket; each PartDesign feature Profile must link to the intended preceding Sketch; do not add an independent top-level solid.

## Source parameters

| Alias | Value | Meaning |
|---|---:|---|
| `PlateLength` | 100 | overall X dimension |
| `PlateWidth` | 80 | overall Y dimension |
| `PlateThick` | 8 | overall Z thickness |
| `MotorPitch` | 31 | NEMA 17 motor-hole square pitch |
| `MotorHoleDia` | 3.5 | motor-hole clearance diameter |
| `PilotDia` | 24 | central motor pilot clearance diameter |
| `SlotWidth` | 7 | M6 clearance-slot width |
| `SlotLength` | 24 | total capsule length, end to end |
| `SlotY` | 30 | Y position of each slot centre, at + and - values |

- The initial and revised projects use unit `mm` and the same nine aliases. The revision changes only the **value** of `Params.SlotLength`; `PlateLength`, `PlateWidth`, `PlateThick`, `MotorPitch`, `MotorHoleDia`, `PilotDia`, `SlotWidth`, and `SlotY` remain unchanged. A revision may add a project-local edit script and evidence files; “only SlotLength changes” is a parameter/constraint definition claim, not a file-tree claim.

## Geometry acceptance, initial revision

- One valid final solid.
- Pilot: through `Ø24` at `(0, 0)`.
- Motor holes: four through `Ø3.5` at `(±15.5, ±15.5)`.
- Frame slots: two horizontal capsules, each total length `24`, width `7`, centres `(0, +30)` and `(0, -30)`.
- Expected volume is `57553.257022909158 mm^3` when independently derived from the parameter table and formula below. Acceptance uses `max(1e-6 mm^3, 1e-9 * expected_volume)`; do not use the model script's own stored expected value as the evaluator's source of truth.


```text
100 * 80 * 8
- pi * (24 / 2)^2 * 8
- 4 * pi * (3.5 / 2)^2 * 8
- 2 * (7 * (24 - 7) + pi * (7 / 2)^2) * 8
= 57553.2570229 mm^3
```

Use a named tolerance and task-local assertion. The assertion does not substitute for inspecting output geometry. Each of `BaseSketch`, `MotorSketch`, and `SlotSketch` must finish with `DoF == 0`, no reported conflicting constraints, and no reported redundant constraints. Geometry acceptance is additionally checked from top and bottom planar inner wires; a cylinder-face count is only a diagnostic, never a hole count.

## Revision task

After the initial model is committed, change **only** the `Params.SlotLength` source from `24` to `30`, recompute, re-observe and commit a second revision.

Acceptance after revision:

- Plate outer dimensions, pilot and motor pattern remain unchanged.
- Each slot is `30 x 7` overall.
- Final expected volume is `56881.257022909158 mm^3` (initial volume minus exactly `672 mm^3`), evaluated with the same tolerance rule.
- The source parameter, SlotSketch constraint geometry and final result must change; the unchanged aliases and BaseSketch/MotorSketch definitions must remain equivalent.
- The evaluator may perturb a copied revision's source `SlotWidth` and verify expression-driven recomputation, then discard that copy. This verifies parameter activity without altering the submitted project.

## Required evidence and V3 workflow

1. Write `notes/design.md` that names the intended function and declares the formula above.
2. `cad3 run --step`, `cad3 inspect --detail`, `cad3 inspect --format code`, `cad3 render --views iso,top,front,right`, then commit initial revision.
3. Execute the revision through a second project-local FreeCAD script or an explicitly changed entrypoint; do not recreate a frozen final BRep.
4. Re-run observation/rendering and commit the second revision.
5. Show `cad3 diff` and `cad3 log`.
6. With a clean project, `cad3 restore <initial-revision>`; inspect the restored FCStd without rebuilding. Confirm initial parameter and initial volume. The restore should leave HEAD unchanged.

## Evaluator checks

The evaluator reads actual `FCStd`, artifacts and Git state. Screenshots support review but do not prove dimensions. A valid BRep alone is insufficient: the feature tree, source parameters, actual geometry, revision propagation, evidence/version correspondence and recovery are all assessed separately.
