"""Workspace filtering keeps permissions and acceptance boundaries explicit."""
import shutil
import json
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
MODULE = Path(__file__).resolve().parents[1] / "src/infermatrix_copilot/rfc_service/web/roadmap-workspace.mjs"


@pytest.mark.skipif(not NODE, reason="Node is required for shipped module syntax")
def test_shipped_frontend_modules_parse_as_modules():
    # Explicit module parsing also checks app.js on Node versions whose
    # automatic CommonJS/module detection can return success without parsing it.
    for path in [MODULE.parent / "app.js", *MODULE.parent.glob("*.mjs")]:
        result = subprocess.run([NODE, "--input-type=module", "--check"],
                                input=path.read_text(encoding="utf-8"),
                                capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, f"{path.name}: {result.stderr}"


def test_offline_interface_catalog_covers_every_shipped_label():
    from infermatrix_copilot.rfc_service.translations import bundled_ui, ui_strings
    missing = set(ui_strings()) - set(bundled_ui())
    assert not missing, "Missing offline translations: " + json.dumps(sorted(missing), ensure_ascii=False)


@pytest.mark.skipif(not NODE, reason="Node is required for browser projections")
def test_workspace_tracking_projection():
    program = f"import {{filterWork, linkedCriteria, workspaceDefaults}} from {MODULE.as_uri()!r};\n" + r'''
import assert from 'node:assert/strict';
const features = [
 {id:'A',title:'Same task',track:'Engine',owner:'Alice',implementation:'implemented'},
 {id:'B',title:'Same task',track:'Models',owner:'',state:'planned'},
 {id:'C',title:'Removed',track:'Engine',owner:'Alice',dropped:true,state:'implemented'}
];
const before=JSON.stringify(features);
assert.deepEqual(filterWork(features).map(f=>f.id),['A','B']);
assert.deepEqual(filterWork(features,{query:'same',track:'Engine',status:'implemented',owner:'Alice'}).map(f=>f.id),['A']);
assert.deepEqual(filterWork(features,{query:'b',owner:'__unassigned'}).map(f=>f.id),['B']);
assert.deepEqual(filterWork(features,{removed:true}).map(f=>f.id),['C']);
assert.deepEqual(filterWork(features,{removed:true,query:'Alice'}).map(f=>f.id),['C']);
const criteria=[{id:'global'},{id:'A1',feature_ids:['A']},{id:'B1',features:['B']},{id:'A2',feature_id:'A'},{id:'AB',feature_ids:['A','B']}];
assert.deepEqual(linkedCriteria(criteria,['A']).map(c=>c.id),['A1','A2','AB']);
assert.deepEqual(linkedCriteria(criteria,['A','B']).map(c=>c.id),['A1','B1','A2','AB']);
assert.equal(linkedCriteria(criteria,[]).length,0);
assert.equal(JSON.stringify(features),before);
const first=workspaceDefaults(), second=workspaceDefaults();
first.viewport.push({left:100}); assert.equal(second.viewport.length,0);
assert.equal(first.tab,'work'); assert.equal(first.mode,'graph'); assert.equal(first.removed,false);
'''
    result = subprocess.run([NODE, "--input-type=module"], input=program, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
