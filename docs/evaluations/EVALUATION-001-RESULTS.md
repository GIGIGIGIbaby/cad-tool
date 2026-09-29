# V3 Evaluation 001 Results

## Scope and method

The task was the NEMA 17 adjustable motor plate in `NEMA17-EVAL-PROMPT.md`. Three independent temporary projects were evaluated:

| Run | Project | Available route / budget | Independent result |
|---|---|---|---|
| High | `/tmp/cad3-eval-deepseek-flash` | `deepseek-official/deepseek-flash`, high | 52/52 checks passed |
| Low | `/tmp/cad3-eval-deepseek-flash-low` | `deepseek-official/deepseek-flash`, low | 52/52 checks passed |
| Inherited | `/tmp/cad3-eval-inherited-route` | inherited current session route; exact provider was not exposed in this session | 52/52 checks passed |

The current DSH session could only invoke `deepseek-official/deepseek-flash`; its model whitelist was fixed when the top-level session was created. I temporarily added `x1api/gpt-6-astra` and `zai-coding-cn/glm-5.3` to the profile whitelist for future top-level sessions, confirmed that the current session did not hot-reload them, and restored the current profile file exactly before continuing. A genuinely cross-provider comparison therefore remains to be run in new top-level sessions.

The evaluator did not trust the agents' JSON metrics or assertions as geometric truth. It checked each project's own Git history, extracted each committed `models/main.FCStd` blob, and opened that byte sequence in an isolated FreeCADCmd process. It independently read the native Body, feature types, Profile links, Spreadsheet values, ExpressionEngine links, sketch diagnostics, solid validity, bounding box, volume, center of gravity, and top/bottom planar inner wires. It also checked the revision parameter delta, the report-to-model hash, the parent commit relation, and restore on a disposable clone.

## Results

All three projects produced the intended native structure:

```text
Params -> MotorPlate(PartDesign::Body)
BaseSketch -> BasePad -> MotorSketch -> MotorPocket -> SlotSketch -> SlotPocket
```

For every project and both revisions, the independent probe found:

- one valid closed final solid;
- bounding box `100 x 80 x 8`;
- 7 expected top inner wires and 7 expected bottom inner wires;
- correct pilot, motor-hole and capsule-slot ring areas/centres/bounds;
- all three sketches at `DoF == 0`, with no reported conflict or redundancy;
- the Body Tip at the final Pocket and native PartDesign Profile links;
- the source parameters and expression chain;
- no independent top-level solid competing with the Body output;
- no frozen `Part::Feature` used as the output.

The independently measured volumes were:

| Run | Initial revision | Revised revision | Restored initial |
|---|---:|---:|---:|
| High | 57553.257022909165 | 56881.257022909170 | 57553.257022909165 |
| Low | 57553.257022909170 | 56881.257022909170 | 57553.257022909170 |
| Inherited | 57553.257022909170 | 56881.257022888140 | 57553.257022909170 |

The expected values were independently derived from the task parameters:

- initial: `57553.257022909158 mm^3`;
- revised: `56881.257022909158 mm^3`;
- exact change: `672 mm^3`.

The inherited run's revised value differs from the analytic value by about `2.1e-8 mm^3`, well inside the fixed evaluator tolerance. It is not treated as exact machine identity.

All three projects had exactly two commits with an initial revision as the direct parent of the revised revision. All three passed restore checks: HEAD stayed on the revised commit, the restored model blob matched the initial commit byte-for-byte, and a fresh FreeCAD probe read the initial parameter and volume without rebuilding the model.

## What the agents actually encountered

The high and low runs both reached the correct result only after real debugging. They encountered:

- rank-deficient capsule constraints where `solve()==0` still left degrees of freedom;
- redundant/tangent constraints producing invalid or null Pocket shapes;
- invalid expression paths;
- the distinction between cumulative PartDesign feature `Shape.Volume` and successive cut volume;
- `Sketch.Shape.Area == 0` on this FreeCAD 1.1.3 console build, requiring profile wire reconstruction for area checks;
- V3 restore refusing dirty worktrees and deleting files absent from the target revision;
- V3 file-level diff truncation on large binary/text output;
- generic no-solid warnings for sketches/datum objects.

These are useful observations because the agents did not merely produce a final box and report success; they had to use the solver state, native feature semantics and V3 failure reports to recover.

## What this does and does not prove

It demonstrates that a current DeepSeek Flash agent can independently use the V3 interface to produce a moderately complex native FreeCAD model, revise a source parameter through the expression chain, generate observations/renders, commit both states and recover the first state.

It does not establish:

- superiority between different model families, because the current top-level session could not dynamically enable the other routes;
- manufacturing correctness, strength, tolerance suitability or real NEMA17/fastener compliance;
- that the agent actually looked at every rendered image, because image inspection is not independently provable from the committed files;
- full semantic CAD diff or merge;
- complete script-to-run provenance after cloning, because `.cad3/runs` is intentionally ignored;
- that the agents could generalize from this task to arbitrary assemblies, CAM or FEM.

The most useful result is therefore architectural: V3's thin execution/observation/version layer was enough for a capable model to use FreeCAD's native Sketcher and PartDesign directly. The model needed several rounds of debugging, but the missing pieces were observable FreeCAD semantics and task-specific reasoning rather than another custom CAD abstraction.
