# V3 Evaluation 003 Results — requirements-only XY cross table (GPT-6 Sol, high)

Task: [EVAL-003-XY-TABLE-PROMPT.md](EVAL-003-XY-TABLE-PROMPT.md). Independent check: `evaluate_eval002.py PROJECT --axes PosX:StrokeX,PosY:StrokeY` (all four corners, saved FCStd reopened without the agent's source, unit-preserving alias writes).

| Item | Independent result |
|---|---|
| Architecture | per axis 2×MGN12 rails, 4×MGN12H blocks, TR8×2 screw, NEMA17 direct drive; Y axis rides on X carriage |
| Parts | 36 valid single solids |
| `PosX` moves | 25 parts: X carriage, blocks, nut **and the whole Y axis incl. Y motor/screw** ✓ |
| `PosY` moves | 7 parts: Y carriage, 4 blocks, nut and nut seat ✓ |
| Interference, 4 corners × 2 revisions | 0 ✓ |
| Swept envelope 150/150 → 200/150 | 335×335×94 ✓ → 385×335×94 |
| Standard dimensions | MGN12 12×8, pitch 25; MGN12H 45.4×27×13; NEMA17 31 mm hole square, Ø22 pilot — correct |
| Self-report | matched; the agent found and fixed a 78.48 mm³ coupling/plate clash itself, used section views |

Engineering review: kinematic hierarchy, load path through four blocks per axis and the parametric chain are right. The serious gap, which the agent stated itself, is that **neither lead screw has end bearings** — each screw hangs off the motor shaft through the coupling, so axial load goes into the motor bearings. Not buildable as is. Also unverified: motor torque margin at 900 rpm, stiffness under 3 kg offset load.

Tool observation: section images use flat, unlit pastel colours; overlaps (none here) would be red, but part boundaries in dense stacks are hard to separate by colour alone.
