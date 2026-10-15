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


@pytest.mark.skipif(not NODE, reason="Node is required for browser checkpoints")
def test_workspace_checkpoint_authorized_scope_and_cleanup():
    program = f"import {{createWorkspaceCheckpoint}} from {MODULE.as_uri()!r};\n" + r'''
import assert from 'node:assert/strict';
const values=new Map();
const storage={get length(){return values.size},key(index){return [...values.keys()][index]},getItem(key){return values.get(key)??null},setItem(key,value){values.set(key,value)},removeItem(key){values.delete(key)}};
const checkpoints=createWorkspaceCheckpoint({storage});
const scope={user_id:'alice',repo_id:'repo-A',rfc_id:'RFC-1'};
checkpoints.save(scope,{tab:'work',query:'Engine',owner:'alice',selection:['F1','F2','F1'],viewport:[{left:130,top:80,scale:1.6}],token:'NEVER',body:'private text not needed for a viewport'});
const restored=checkpoints.restore(scope);
assert.equal(restored.query,'Engine');assert.deepEqual(restored.selection,['F1','F2']);
assert.deepEqual(restored.viewport,[{left:130,top:80,scale:1.6}]);
assert(!('token' in restored));assert(!('body' in restored));assert(![...values.values()].join('').includes('NEVER'));
assert.equal(checkpoints.restore({...scope,user_id:'bob'}),null);
assert.equal(checkpoints.restore({...scope,repo_id:'repo-B'}),null);
assert.equal(checkpoints.restore({...scope,rfc_id:'RFC-2'}),null);
checkpoints.save({user_id:'alice',rfc_id:'RFC-no-authorized-repo'},{selection:['F1']});assert.equal(values.size,1);
checkpoints.save({...scope,repo_id:'repo-B'}, {query:'other repository'});
checkpoints.save({...scope,rfc_id:'RFC-2'}, {query:'keep this'});
checkpoints.forget({user_id:'alice',rfc_id:'RFC-1'});
assert.equal(checkpoints.restore(scope),null);assert.equal(checkpoints.restore({...scope,repo_id:'repo-B'}),null);
assert.equal(checkpoints.restore({...scope,rfc_id:'RFC-2'}).query,'keep this');
checkpoints.save(scope,{viewport:[{left:-10,top:Infinity,scale:100}],selection:[null,3,'valid'],query:'x'.repeat(3000)});
assert.deepEqual(checkpoints.restore(scope).viewport,[{left:0,top:0,scale:2.5}]);
assert.deepEqual(checkpoints.restore(scope).selection,['valid']);assert.equal(checkpoints.restore(scope).query.length,2000);
values.set('unrelated-setting','keep');checkpoints.clear();assert.equal(values.size,1);assert.equal(values.get('unrelated-setting'),'keep');
const unavailable=createWorkspaceCheckpoint({storage:{getItem(){throw new Error('disabled')},setItem(){throw new Error('quota')},get length(){throw new Error('disabled')}}});
assert.equal(unavailable.restore(scope),null);unavailable.save(scope,{query:'safe'});unavailable.forget(scope);unavailable.clear();
'''
    result = subprocess.run([NODE, "--input-type=module"], input=program, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not NODE, reason="Node is required for private composer checkpoints")
def test_chat_input_checkpoint_contains_only_scoped_unsent_text():
    program = f"import {{createChatInputCheckpoint, createWorkspaceCheckpoint}} from {MODULE.as_uri()!r};\n" + r'''
import assert from 'node:assert/strict';
const values=new Map();
const storage={get length(){return values.size},key(index){return [...values.keys()][index]},getItem(key){return values.get(key)??null},setItem(key,value){values.set(key,value)},removeItem(key){values.delete(key)}};
const composer=createChatInputCheckpoint({storage}), workspace=createWorkspaceCheckpoint({storage});
const scope={user_id:'alice',repo_id:'repo-A',rfc_id:'RFC-1'};
composer.save(scope,'Unsent proposal context 中文');
assert.equal(composer.restore(scope),'Unsent proposal context 中文');
assert.equal(composer.restore({...scope,user_id:'bob'}),null);
assert.equal(composer.restore({...scope,repo_id:'repo-B'}),null);
assert.equal(composer.restore({...scope,rfc_id:'RFC-2'}),null);
composer.save({...scope,rfc_id:'RFC-2'},'Keep second RFC');
composer.save(scope,{token:'secret',transcript:'do not persist',proposal:{}});
assert.equal(composer.restore(scope),'Unsent proposal context 中文');
composer.save(scope,'x'.repeat(20001));assert.equal(composer.restore(scope).length,20000);
workspace.save(scope,{query:'independent graph state'});
composer.forget({user_id:'alice',rfc_id:'RFC-1'});assert.equal(composer.restore(scope),null);
assert.equal(composer.restore({...scope,rfc_id:'RFC-2'}),'Keep second RFC');
assert.equal(workspace.restore(scope).query,'independent graph state');
composer.clear();assert.equal(composer.restore({...scope,rfc_id:'RFC-2'}),null);
assert.equal(workspace.restore(scope).query,'independent graph state');
const unavailable=createChatInputCheckpoint({storage:{getItem(){throw new Error('disabled')},setItem(){throw new Error('quota')},get length(){throw new Error('disabled')}}});
assert.equal(unavailable.restore(scope),null);unavailable.save(scope,'editing still works');unavailable.forget(scope);unavailable.clear();
'''
    result = subprocess.run([NODE, "--input-type=module"], input=program, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
