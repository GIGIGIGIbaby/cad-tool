#!/usr/bin/env python3
"""Independent evaluator for V3 Evaluation 001.

This host script never executes a candidate's model scripts. It extracts the
committed FCStd blobs, probes them in fresh isolated FreeCADCmd processes, and
checks project Git history, metadata consistency, native structure and geometry.
It deliberately treats artifacts as evidence hints, not geometric truth.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import Any

PARAMS = {
    "PlateLength": 100.0,
    "PlateWidth": 80.0,
    "PlateThick": 8.0,
    "MotorPitch": 31.0,
    "MotorHoleDia": 3.5,
    "PilotDia": 24.0,
    "SlotWidth": 7.0,
    "SlotY": 30.0,
}
EXPECTED = {
    24.0: 57553.257022909158,
    30.0: 56881.257022909158,
}
TOL = 1e-6
PROTOCOL = "cad3.eval-probe/v1"

PROBE = r'''import FreeCAD as App
import Part
import json, math, os, sys, traceback
from pathlib import Path

INPUT = Path(os.environ['CAD3_EVAL_INPUT'])
OUTPUT = Path(os.environ['CAD3_EVAL_OUTPUT'])

def finite(value):
    return isinstance(value, (int, float)) and math.isfinite(float(value))

def num(value):
    try:
        return float(value)
    except Exception:
        return None

def vector(v):
    return [float(v.x), float(v.y), float(v.z)]

def bbox(shape):
    b = shape.BoundBox
    return [float(b.XMin),float(b.YMin),float(b.ZMin),float(b.XLength),float(b.YLength),float(b.ZLength)]

def link_name(value):
    if isinstance(value, (tuple, list)):
        if not value: return None
        return link_name(value[0])
    return getattr(value, 'Name', None)

def face_rings(shape, z_value):
    rings=[]
    for idx, face in enumerate(shape.Faces, 1):
        try:
            if face.Surface.TypeId != 'Part::GeomPlane':
                continue
            b=face.BoundBox
            if abs(b.ZMin-z_value)>1e-6 or abs(b.ZMax-z_value)>1e-6:
                continue
            wires=list(face.Wires)
            for wire in wires[1:]:
                f=Part.Face(wire)
                rings.append({'face_index':idx,'area_mm2':float(f.Area),'center':vector(f.CenterOfMass),'bbox':bbox(f),'edge_count':len(wire.Edges)})
        except Exception as exc:
            rings.append({'error':type(exc).__name__+': '+str(exc)})
    return rings

def sketch(obj):
    info={'name':obj.Name,'type':obj.TypeId,'dof':getattr(obj,'DoF',None),'fully_constrained':getattr(obj,'FullyConstrained',None),'conflicting':list(getattr(obj,'ConflictingConstraints',[]) or []),'redundant':list(getattr(obj,'RedundantConstraints',[]) or []),'partially_redundant':list(getattr(obj,'PartiallyRedundantConstraints',[]) or []),'expression_engine':[list(x) for x in (getattr(obj,'ExpressionEngine',[]) or [])]}
    constraints=[]
    for i,c in enumerate(list(getattr(obj,'Constraints',[]) or [])):
        constraints.append({'index':i,'name':getattr(c,'Name',''),'type':getattr(c,'Type',''),'value':num(getattr(c,'Value',None))})
    info['constraints']=constraints
    geom=[]
    for i,g in enumerate(list(getattr(obj,'Geometry',[]) or [])):
        item={'index':i,'type':getattr(g,'TypeId',type(g).__name__)}
        for name in ('StartPoint','EndPoint','Center'):
            if hasattr(g,name):
                try: item[name]=vector(getattr(g,name))
                except Exception: pass
        if hasattr(g,'Radius'): item['radius']=num(g.Radius)
        geom.append(item)
    info['geometry']=geom
    return info

result={'protocol':'cad3.eval-probe/v1','ok':False,'source':str(INPUT),'raw':{},'recomputed':{}}
try:
    doc=App.openDocument(str(INPUT))
    body=doc.getObject('MotorPlate')
    sheet=doc.getObject('Params')
    result['raw']['object_states']={o.Name:list(getattr(o,'State',[]) or []) for o in doc.Objects}
    result['raw']['body_shape_before_recompute']= None if body is None else {'volume_mm3':num(body.Shape.Volume),'bbox':bbox(body.Shape),'valid':bool(body.Shape.isValid()),'solids':len(body.Shape.Solids)}
    doc.recompute()
    if body is None or sheet is None:
        raise RuntimeError('missing MotorPlate Body or Params Spreadsheet')
    shape=body.Shape
    params={}
    for row in range(1, 30):
        cell='B%d'%row
        alias=sheet.getAlias(cell)
        if alias:
            params[alias]={'cell':cell,'raw':sheet.getContents(cell),'value':num(sheet.get(cell))}
    group=list(getattr(body,'Group',[]) or [])
    features=[]
    sketches=[]
    top_level_solids=[]
    group_names={o.Name for o in group}
    for obj in doc.Objects:
        item={'name':obj.Name,'type':obj.TypeId,'label':obj.Label,'state':list(getattr(obj,'State',[]) or [])}
        if obj.Name in group_names:
            item['in_body']=True
        if obj.TypeId.startswith('PartDesign::') and hasattr(obj,'Profile'):
            item['profile']=link_name(obj.Profile)
        if obj.TypeId.startswith('PartDesign::'):
            features.append(item)
        if obj.TypeId.startswith('Sketcher::'):
            sketches.append(sketch(obj))
        try:
            if obj.Name not in group_names and obj is not body and hasattr(obj,'Shape') and not obj.Shape.isNull() and len(obj.Shape.Solids):
                top_level_solids.append({'name':obj.Name,'type':obj.TypeId,'solids':len(obj.Shape.Solids)})
        except Exception: pass
    b=shape.BoundBox
    ztop=b.ZMax; zbottom=b.ZMin
    result['recomputed']={
      'body':{'name':body.Name,'type':body.TypeId,'tip':link_name(getattr(body,'Tip',None)),'group':[o.Name for o in group], 'volume_mm3':num(shape.Volume),'area_mm2':num(shape.Area),'bbox':bbox(shape),'center_of_gravity':vector(shape.CenterOfGravity),'valid':bool(shape.isValid()),'closed_solids':[bool(s.isClosed()) for s in shape.Solids],'solids':len(shape.Solids),'faces':len(shape.Faces)},
      'params':params,'features':features,'sketches':sketches,'top_level_solids':top_level_solids,'top_rings':face_rings(shape,ztop),'bottom_rings':face_rings(shape,zbottom),
      'doc_objects':[{'name':o.Name,'type':o.TypeId,'label':o.Label} for o in doc.Objects],
    }
    result['ok']=True
except BaseException as exc:
    result['error']={'type':type(exc).__name__,'message':str(exc),'traceback':traceback.format_exc()}
finally:
    OUTPUT.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
'''


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git(root: Path, *args: str, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=text, check=False, env={**os.environ, "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1"})


def check(report: dict, check_id: str, passed: bool, expected: Any, observed: Any, note: str = "") -> None:
    report["checks"].append({"id":check_id,"passed":bool(passed),"expected":expected,"observed":observed,"note":note})


def blob(root: Path, rev: str, path: str) -> bytes | None:
    result=git(root,"show",f"{rev}:{path}",text=False)
    return result.stdout if result.returncode==0 else None


def json_blob(root: Path, rev: str, path: str) -> dict | None:
    data=blob(root,rev,path)
    try: return json.loads(data.decode("utf-8")) if data else None
    except Exception: return None


def discover(explicit: str | None) -> str:
    candidates=[]
    if explicit: candidates.append(explicit)
    if os.environ.get("CAD3_FREECAD"): candidates.append(os.environ["CAD3_FREECAD"])
    candidates.extend(["/Applications/FreeCAD.app/Contents/Resources/bin/freecadcmd", shutil.which("freecadcmd") or ""])
    for value in candidates:
        if value and Path(value).is_file() and os.access(value,os.X_OK): return str(Path(value))
    raise RuntimeError("FreeCADCmd was not found; pass --freecad")


def run_probe(executable: str, fcstd: Path, work: Path, label: str) -> dict:
    run=work/label; run.mkdir(parents=True,exist_ok=True)
    script=run/"probe.py"; output=run/"result.json"; profile=run/"profile"; home=run/"home"
    script.write_text(PROBE,encoding="utf-8")
    env=os.environ.copy(); env.update({"CAD3_EVAL_INPUT":str(fcstd),"CAD3_EVAL_OUTPUT":str(output),"FREECAD_USER_HOME":str(profile),"HOME":str(home),"PYTHONIOENCODING":"utf-8"})
    for name in ("PYTHONHOME","PYTHONPATH","VIRTUAL_ENV"): env.pop(name,None)
    proc=subprocess.run([executable,"--user-cfg",str(profile/"user.cfg"),"--system-cfg",str(profile/"system.cfg"),str(script)],capture_output=True,text=True,env=env,timeout=90)
    (run/"stdout.log").write_text(proc.stdout,encoding="utf-8",errors="replace")
    (run/"stderr.log").write_text(proc.stderr,encoding="utf-8",errors="replace")
    data=json.loads(output.read_text()) if output.exists() else {"ok":False,"error":{"type":"MissingResult","message":"probe did not write JSON"}}
    data["process_exit_code"]=proc.returncode; data["probe_dir"]=str(run)
    return data


def close(a: float | None, b: float, tol: float=TOL) -> bool:
    return a is not None and abs(a-b)<=max(tol,abs(b)*1e-9)


def rings_ok(rings: list[dict], slot_length: float) -> tuple[bool, list[str]]:
    expected=[]
    expected.append((math.pi*12**2,(0.0,0.0),(24.0,24.0),'pilot'))
    for x in (-15.5,15.5):
        for y in (-15.5,15.5): expected.append((math.pi*1.75**2,(x,y),(3.5,3.5),'motor'))
    slot_area=7*(slot_length-7)+math.pi*3.5**2
    for y in (-30.0,30.0): expected.append((slot_area,(0.0,y),(slot_length,7.0),'slot'))
    remaining=list(rings); messages=[]
    for area,center,size,kind in expected:
        match=None
        for i,r in enumerate(remaining):
            c=r.get('center',[]); b=r.get('bbox',[])
            if len(c)==3 and len(b)==6 and close(r.get('area_mm2'),area) and close(c[0],center[0]) and close(c[1],center[1]) and close(b[3],size[0]) and close(b[4],size[1]):
                match=i; break
        if match is None: messages.append('missing '+kind)
        else: remaining.pop(match)
    if remaining: messages.append('unexpected rings='+str(len(remaining)))
    return not messages,messages


def evaluate_revision(root: Path, revision: str, fcstd_bytes: bytes, executable: str, scratch: Path, expected_slot: float, report: dict) -> dict:
    file=scratch/(revision[:12]+".FCStd"); file.write_bytes(fcstd_bytes)
    raw=run_probe(executable,file,scratch,revision[:12])
    check(report,f"{revision[:7]}.probe_protocol",raw.get('ok') and raw.get('process_exit_code')==0,True,{"ok":raw.get('ok'),"exit":raw.get('process_exit_code')})
    state=raw.get('recomputed',{}); body=state.get('body',{}); params=state.get('params',{})
    expected_volume=EXPECTED[expected_slot]
    check(report,f"{revision[:7]}.body_name",body.get('name')=='MotorPlate','MotorPlate',body.get('name'))
    check(report,f"{revision[:7]}.body_type",body.get('type')=='PartDesign::Body','PartDesign::Body',body.get('type'))
    check(report,f"{revision[:7]}.solid_valid",body.get('solids')==1 and body.get('valid') and body.get('closed_solids')==[True],{"solids":1,"valid":True,"closed":[True]},{"solids":body.get('solids'),"valid":body.get('valid'),"closed":body.get('closed_solids')})
    check(report,f"{revision[:7]}.bbox",all(close(x,y) for x,y in zip((body.get('bbox') or [None]*6)[3:],(100.,80.,8.))),[100,80,8],(body.get('bbox') or [])[3:])
    cog=body.get('center_of_gravity') or []
    body_bbox = body.get('bbox') or []
    expected_zmid = body_bbox[2] + body_bbox[5] / 2 if len(body_bbox) == 6 else None
    check(report,f"{revision[:7]}.centred",len(cog)==3 and close(cog[0],0) and close(cog[1],0) and close(cog[2],expected_zmid if expected_zmid is not None else float('nan')),[0,0,'zmin+ZLength/2'],cog)
    check(report,f"{revision[:7]}.volume",close(body.get('volume_mm3'),expected_volume),expected_volume,body.get('volume_mm3'))
    check(report,f"{revision[:7]}.outputs_only_body",not state.get('top_level_solids'),[],state.get('top_level_solids'))
    check(report,f"{revision[:7]}.tip_pocket",body.get('tip') and any(f.get('name')==body.get('tip') and f.get('type')=='PartDesign::Pocket' for f in state.get('features',[])),'PartDesign::Pocket',body.get('tip'))
    types=[f.get('type') for f in state.get('features',[])]
    check(report,f"{revision[:7]}.feature_chain_types",types.count('PartDesign::Pad')>=1 and types.count('PartDesign::Pocket')>=2,{'Pad':'>=1','Pocket':'>=2'},types)
    profiles={f.get('name'):f.get('profile') for f in state.get('features',[]) if f.get('profile')}
    check(report,f"{revision[:7]}.feature_profiles",len(set(profiles.values()))>=3,'Pad and two Pockets with separate sketch Profiles',profiles)
    wanted={**PARAMS,'SlotLength':expected_slot}
    observed={name:data.get('value') for name,data in params.items()}
    check(report,f"{revision[:7]}.parameter_values",all(close(observed.get(k),v) for k,v in wanted.items()),wanted,observed)
    expression_targets=[]
    for sketch in state.get('sketches',[]): expression_targets.extend(sketch.get('expression_engine',[]))
    check(report,f"{revision[:7]}.expression_links",len(expression_targets)>0,'one or more expression links',expression_targets[:20])
    diagnostics={s.get('name'):{'dof':s.get('dof'),'conflicting':s.get('conflicting'),'redundant':s.get('redundant')} for s in state.get('sketches',[])}
    required_sketches=['BaseSketch','MotorSketch','SlotSketch']
    check(report,f"{revision[:7]}.sketch_diagnostics",all(diagnostics.get(n,{}).get('dof')==0 and not diagnostics.get(n,{}).get('conflicting') and not diagnostics.get(n,{}).get('redundant') for n in required_sketches),{'DoF':0,'conflicting':[],'redundant':[]},diagnostics)
    top=state.get('top_rings',[]); bottom=state.get('bottom_rings',[])
    top_ok,top_messages=rings_ok(top,expected_slot); bottom_ok,bottom_messages=rings_ok(bottom,expected_slot)
    check(report,f"{revision[:7]}.top_openings",len(top)==7 and top_ok,'seven expected top planar inner wires',{'count':len(top),'messages':top_messages})
    check(report,f"{revision[:7]}.bottom_openings",len(bottom)==7 and bottom_ok,'seven expected bottom planar inner wires',{'count':len(bottom),'messages':bottom_messages})
    return raw


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('project',type=Path)
    ap.add_argument('--freecad')
    ap.add_argument('--keep-workdir',action='store_true')
    args=ap.parse_args()
    root=args.project.resolve(); report={'schema':'cad3.evaluation/nema17-v1','project':str(root),'checks':[],'warnings':[],'manual_review':[],'unverifiable':[]}
    if not (root/'.git').is_dir(): raise SystemExit('project needs its own .git directory')
    top=git(root,'rev-parse','--show-toplevel').stdout.strip(); check(report,'git.own_repository',top==str(root),str(root),top)
    commits=[line for line in git(root,'rev-list','--reverse','HEAD').stdout.splitlines() if line]
    check(report,'git.two_commits',len(commits)==2,2,commits)
    if len(commits)<2:
        print(json.dumps(report,ensure_ascii=False,indent=2)); return 1
    rev_a,rev_b=commits[-2:]
    parent=git(root,'show','-s','--format=%P',rev_b).stdout.strip().split()
    check(report,'git.parent_chain',parent==[rev_a],[rev_a],parent)
    executable=discover(args.freecad)
    scratch=Path(tempfile.mkdtemp(prefix='cad3-nema17-eval-'))
    report['scratch']=str(scratch)
    try:
        blobs={}
        for rev,slot in ((rev_a,24.0),(rev_b,30.0)):
            data=blob(root,rev,'models/main.FCStd'); check(report,f'{rev[:7]}.fcstd_present',data is not None,'models/main.FCStd',bool(data))
            if not data: continue
            blobs[rev]=data
            cfg=json_blob(root,rev,'cad3.json'); check(report,f'{rev[:7]}.project_schema',(cfg or {}).get('schema')=='cad3.project/v1','cad3.project/v1',(cfg or {}).get('schema'))
            design=blob(root,rev,'notes/design.md'); check(report,f'{rev[:7]}.design_note',design is not None,'notes/design.md',bool(design))
            rep=json_blob(root,rev,'artifacts/latest/report.json')
            blob_hash=sha(data)
            if rep is None:
                check(report,f'{rev[:7]}.report_present',False,'artifacts/latest/report.json',None)
            else:
                model=(rep.get('model') or {}).get('sha256')
                check(report,f'{rev[:7]}.report_model_hash',model==blob_hash,blob_hash,model)
                check(report,f'{rev[:7]}.report_protocol',rep.get('ok') is True and rep.get('action')=='execute' and not rep.get('errors') and (rep.get('protocol') or {}).get('completion_status')=='completed',{'ok':True,'action':'execute','errors':[],'completion':'completed'},{'ok':rep.get('ok'),'action':rep.get('action'),'errors':rep.get('errors'),'completion':(rep.get('protocol') or {}).get('completion_status')})
            evaluate_revision(root,rev,data,executable,scratch,slot,report)
        if rev_a in blobs and rev_b in blobs:
            ha,hb=sha(blobs[rev_a]),sha(blobs[rev_b])
            rep_b=json_blob(root,rev_b,'artifacts/latest/report.json') or {}
            check(report,'revision.input_hash_chain',(rep_b.get('input_hash') or {}).get('before')==ha,ha,(rep_b.get('input_hash') or {}).get('before'))
            a_data=run_probe(executable,scratch/(rev_a[:12]+'.FCStd'),scratch,'compare_a').get('recomputed',{})
            b_data=run_probe(executable,scratch/(rev_b[:12]+'.FCStd'),scratch,'compare_b').get('recomputed',{})
            a_params={n:x.get('value') for n,x in a_data.get('params',{}).items()}; b_params={n:x.get('value') for n,x in b_data.get('params',{}).items()}
            changed=[n for n in sorted(set(a_params)|set(b_params)) if a_params.get(n)!=b_params.get(n)]
            check(report,'revision.only_slotlength_parameter_changes',changed==['SlotLength'],['SlotLength'],changed)
            av=(a_data.get('body') or {}).get('volume_mm3'); bv=(b_data.get('body') or {}).get('volume_mm3')
            check(report,'revision.volume_delta',close((av or 0)-(bv or 0),672.0),672.0,(av or 0)-(bv or 0))
        # Restore test runs in a disposable local clone at revision B.
        clone=scratch/'restore-clone'; cloned=subprocess.run(['git','clone','--no-local',str(root),str(clone)],capture_output=True,text=True)
        if cloned.returncode:
            check(report,'restore.clone',False,'local clone',cloned.stderr)
        else:
            tool=Path(__file__).resolve().parents[2]/'cad3.py'
            restore=subprocess.run([sys.executable,str(tool),'restore','-p',str(clone),rev_a],capture_output=True,text=True)
            try: restore_data=json.loads(restore.stdout)
            except Exception: restore_data={}
            restored=(clone/'models/main.FCStd').read_bytes() if (clone/'models/main.FCStd').is_file() else b''
            head=git(clone,'rev-parse','HEAD').stdout.strip()
            check(report,'restore.protocol',restore.returncode==0 and restore_data.get('ok') is True and restore_data.get('head_moved') is False,{'ok':True,'head_moved':False},{'code':restore.returncode,'data':restore_data})
            check(report,'restore.head_unchanged',head==rev_b,rev_b,head)
            check(report,'restore.blob_exact',bool(blobs.get(rev_a)) and sha(restored)==sha(blobs[rev_a]),sha(blobs.get(rev_a,b'')),sha(restored) if restored else None)
            if restored:
                restored_probe=run_probe(executable,clone/'models/main.FCStd',scratch,'restored')
                check(report,'restore.geometry_initial',close(((restored_probe.get('recomputed') or {}).get('body') or {}).get('volume_mm3'),EXPECTED[24.0]),EXPECTED[24.0],((restored_probe.get('recomputed') or {}).get('body') or {}).get('volume_mm3'))
        report['manual_review']=['Review rendering with numeric/structure evidence; it is not a dimension proof.','Review notes/design.md for intent and formula honesty.','Manufacturing strength, tolerance, fastener bearing and actual belt tension are out of scope.']
        report['unverifiable']=['Agent actually viewed images cannot be established from artifacts.','Artifacts can be edited; FCStd blobs probed independently are the geometric evidence.','The ignored .cad3 run directory is not present in a clean clone, so script-to-run provenance is incomplete.']
    finally:
        if not args.keep_workdir: shutil.rmtree(scratch,ignore_errors=True)
    report['passed']=all(item['passed'] for item in report['checks'])
    print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
    return 0 if report['passed'] else 1

if __name__=='__main__': raise SystemExit(main())
