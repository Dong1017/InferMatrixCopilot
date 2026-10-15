"""Real-browser acceptance for daily RFC work, without external services.

Run with ``python -m pip install playwright && python -m playwright install chromium``.
RFC_BROWSER_EXECUTABLE may point at an existing Chromium installation. The
dedicated CI job installs a browser explicitly; other portability jobs may skip.
"""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Install Playwright for browser acceptance")
expect = playwright.expect

from infermatrix_copilot.rfc_service.application import RFCService
from infermatrix_copilot.rfc_service.http import create_server
from infermatrix_copilot.rfc_service.providers import LocalProvider


BODY = """# Team RFC

## Goals

Make daily RFC work reliable and easy to inspect.

## Scope

Track engine and model delivery with independent acceptance.

## Design

Use a persistent workspace and reviewed source writes.

## Work plan

### Engine

#### F1. Build the engine

Owner: Alice

#### F2. Verify the engine

Depends on: F1

### 工作依赖

#### F3. Add the model

Owner: Bob

## Acceptance criteria

- Global delivery check.
- Engine output check.
- Engine validation check.

## Context

```mermaid
flowchart LR
Goal[Interactive output] --> F1[Engine]
F2[Validation] --> Outcome[Verified outcome]
```

## Risks

Provider outages must retain approved edits.

## References

Repository documentation.
"""


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as engine:
        executable = os.environ.get("RFC_BROWSER_EXECUTABLE", "")
        target = Path(executable or engine.chromium.executable_path)
        if not target.is_file():
            pytest.skip("Chromium is absent; run python -m playwright install chromium")
        instance = engine.chromium.launch(executable_path=str(target), headless=True, args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture()
def site(tmp_path):
    documents = tmp_path / "documents"
    documents.mkdir()
    path = documents / "team.md"
    path.write_text(BODY, encoding="utf-8")
    service = RFCService(tmp_path / "state", providers={"local": LocalProvider({})}, roots=[documents])
    token = service.bootstrap_admin("Owner")
    owner = service.authenticate(token["token"])
    repository = service.dispatch(owner, "repositories.create", {
        "name": "Product test", "provider": "local", "external_name": "product-test", "root": str(documents)})
    queued = service.dispatch(owner, "rfcs.enroll", {"repo_id": repository["id"], "source": {
        "provider": "local", "repository": repository["external_name"], "kind": "markdown", "identifier": "team.md", "path": "team.md"}})
    assert service.process_pending()[0]["status"] == "succeeded"
    rfc_id = service.dispatch(owner, "operations.get", {"operation_id": queued["operation_id"]})["result"]["rfc_id"]
    current = service.dispatch(owner, "rfcs.get", {"rfc_id": rfc_id})
    service.dispatch(owner, "rfcs.update", {"rfc_id": rfc_id, "expected_revision": current["revision"], "reason": "Link task acceptance", "criteria": [
        {"id": "C-global", "title": "Global delivery check.", "feature_ids": []},
        {"id": "C-engine", "title": "Engine output check.", "feature_ids": ["F1"]},
        {"id": "C-validation", "title": "Engine validation check.", "feature_ids": ["F2"]}]})
    reader = service.dispatch(owner, "users.create", {"name": "Reader"})
    reader_token = service.dispatch(owner, "tokens.create", {"user_id": reader["id"]})
    service.dispatch(owner, "grants.set", {"repo_id": repository["id"], "user_id": reader["id"], "role": "reader"})
    server = create_server(service)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield SimpleNamespace(service=service, owner=owner, token=token["token"], reader_token=reader_token["token"],
                          rfc_id=rfc_id, repository=repository, source=path, origin=server.public_origin)
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


@pytest.fixture()
def open_page(browser, site):
    contexts = []
    def open(*, reader=False, mobile=False, language="zh", token=None, offline_catalog=False):
        context = browser.new_context(viewport={"width": 390, "height": 844} if mobile else {"width": 1440, "height": 1000})
        contexts.append(context)
        response = context.request.post(site.origin + "/api/v1/session", data={"token": token or (site.reader_token if reader else site.token)}, headers={"Origin": site.origin})
        assert response.ok
        page = context.new_page()
        if offline_catalog:
            page.route("**/roadmap-languages.json?**", lambda route: route.abort("failed"))
        page.set_default_timeout(15000)
        page.add_init_script("""window.prompt = () => { throw new Error('Native prompt is forbidden'); };
            window.confirm = () => { throw new Error('Native confirm is forbidden'); };""")
        page.errors = []
        page.failed_responses = []
        page.on("pageerror", lambda error: page.errors.append(error.stack or str(error)))
        cdp = context.new_cdp_session(page)
        cdp.send("Runtime.enable")
        cdp.on("Runtime.exceptionThrown", lambda event: page.errors.append(str({
            key: event["exceptionDetails"].get(key) for key in ("text", "url", "lineNumber", "columnNumber")})))
        page.on("response", lambda response: page.failed_responses.append((response.status, response.url)) if response.status >= 400 else None)
        page.goto(site.origin + f"/roadmap?lang={language}#rfc/{site.rfc_id}")
        try:
            expect(page.locator(".daily-rfc")).to_be_visible()
        except AssertionError as failure:
            raise AssertionError(f"Workspace did not load: JS={page.errors}; HTTP={page.failed_responses}") from failure
        expect(page.locator("#rfc-work .task-row[data-feature-id=F1]") if mobile else page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first).to_be_visible(timeout=60000)
        return page
    yield open
    for context in contexts:
        context.close()


def current(site):
    return site.service.dispatch(site.owner, "rfcs.get", {"rfc_id": site.rfc_id})


def inspect(page, feature_id):
    page.locator(f"#rfc-work .roadmap-actionable[data-feature-id={feature_id}]").first.click()
    target = page.locator(f"#workspace-inspector .work-item[data-feature-id={feature_id}]")
    expect(target).to_be_visible()
    return target


def confirm_action(page, label, *, reason="Verified in product browser acceptance"):
    dialog = page.locator("dialog.action-dialog[open]")
    expect(dialog).to_be_visible()
    control = dialog.locator("[name=reason]")
    if control.count():
        control.fill(reason)
    dialog.get_by_role("button", name=label, exact=True).click()
    expect(dialog).not_to_be_visible()


def open_agent(page):
    if not page.locator(".workspace-side").is_visible():
        page.locator(".workspace-open-details").click()
    page.locator(".workspace-side-tabs").get_by_role("tab", name="Agent", exact=True).click()
    composer = page.locator("#workspace-agent .chat-composer textarea")
    expect(composer).to_be_editable()
    return composer


def test_graph_is_on_first_screen_and_inspector_contains_only_linked_acceptance(open_page):
    page = open_page()
    canvas = page.locator("#rfc-work .roadmap-canvas:visible").first
    bounds = canvas.bounding_box()
    assert bounds and bounds["y"] < 750 and bounds["y"] + min(bounds["height"], 180) < 1000
    node_bounds = page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first.bounding_box()
    assert node_bounds and node_bounds["y"] < 1000 and node_bounds["y"] + node_bounds["height"] > 0
    expect(page.locator("#rfc-outcome")).to_be_visible()
    expect(page.locator("[data-workspace-tab=work]")).to_have_attribute("aria-selected", "true")
    inspect(page, "F1")
    expect(page.locator("#workspace-inspector .criterion[data-criterion-id=C-engine]")).to_be_visible()
    expect(page.locator("#workspace-inspector .criterion[data-criterion-id=C-global]")).to_have_count(0)
    expect(page.locator("#workspace-inspector .criterion[data-criterion-id=C-validation]")).to_have_count(0)
    page.locator("[data-workspace-tab=acceptance]").click()
    expect(page.locator("#rfc-acceptance .criterion[data-criterion-id=C-global]")).to_be_visible()
    page.locator("[data-workspace-tab=work]").click()
    page.locator("#rfc-work select[name=diagram]").select_option("__all")
    page.locator("#rfc-work .roadmap-actionable[data-feature-id=''][data-group-id='']").first.click()
    expect(page.locator("#workspace-inspector")).to_contain_text("来源上下文")
    expect(page.locator("#workspace-inspector .work-item")).to_have_count(0)
    assert not page.errors


def test_mark_done_remove_and_restore_work_without_native_dialogs(open_page, site):
    page = open_page()
    item = inspect(page, "F1")
    page.evaluate("window.acceptanceSVG = document.querySelector('#rfc-work .roadmap-track svg')")
    item.get_by_role("button", name="标记实现完成", exact=True).click()
    assert current(site)["features"][0]["implementation"] == "planned"
    confirm_action(page, "确认实现完成")
    node = page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first
    expect(node).to_have_class(re.compile(r"\bimplemented\b"))
    assert current(site)["acceptance"] == "pending"
    assert page.evaluate("acceptanceSVG === document.querySelector('#rfc-work .roadmap-track svg')")
    page.locator("#workspace-inspector .work-item[data-feature-id=F1]").get_by_role("button", name="移除", exact=True).click()
    confirm_action(page, "确认移除")
    expect(page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]")).to_have_count(0)
    page.locator("#rfc-work").get_by_role("button", name="已移除", exact=True).click()
    removed = page.locator(".task-row[data-feature-id=F1]")
    expect(removed).to_be_visible()
    removed.get_by_role("button", name="恢复", exact=True).click()
    confirm_action(page, "确认恢复")
    expect(page.locator(".task-row[data-feature-id=F1]")).to_have_count(0)
    page.locator("#rfc-work").get_by_role("button", name="路线图", exact=True).click()
    expect(page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first).to_be_visible()
    assert site.source.read_text(encoding="utf-8") == BODY and not page.errors


def test_ctrl_selection_merge_and_split_keep_original_tasks(open_page, site):
    page = open_page()
    page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first.click(modifiers=["Control"])
    page.locator("#rfc-work .roadmap-actionable[data-feature-id=F2]").first.click(modifiers=["Meta"])
    expect(page.locator(".workspace-selection .graph-selection-count")).to_contain_text("2")
    assert current(site).get("node_groups", []) == []
    page.locator(".workspace-selection").get_by_role("button", name="合并选中节点", exact=True).click()
    dialog = page.locator("dialog.graph-merge[open]")
    expect(dialog.locator("input[value=F1]")).to_be_checked()
    expect(dialog.locator("input[value=F2]")).to_be_checked()
    dialog.locator("input[name=title]").fill("Engine delivery")
    dialog.get_by_role("button", name="合并选中节点", exact=True).click()
    expect(dialog).not_to_be_visible()
    group = current(site)["node_groups"][0]
    assert set(group["feature_ids"]) == {"F1", "F2"}
    page.locator(f"#rfc-work .roadmap-actionable[data-group-id='{group['id']}']").first.click()
    page.locator("#workspace-inspector").get_by_role("button", name="拆分节点", exact=True).click()
    confirm_action(page, "确认拆分")
    expect(page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first).to_be_visible()
    assert current(site)["node_groups"] == [] and len(current(site)["features"]) == 3
    assert current(site)["body"] == BODY and not page.errors


def test_graph_drag_zoom_and_keyboard_inspection_survive_refresh(open_page):
    page = open_page()
    graph = page.locator("#rfc-work .roadmap-track:visible").first
    canvas = graph.locator(".roadmap-canvas")
    for _ in range(6):
        graph.locator('button[aria-label$="放大"]').click()
    assert canvas.evaluate("node => node.scrollWidth > node.clientWidth")
    bounds = canvas.bounding_box()
    # The top padding is empty canvas even after zooming; lower nodes may be
    # below the viewport and are intentionally excluded from drag initiation.
    y = bounds["y"] + 10
    page.mouse.move(bounds["x"] + bounds["width"] - 40, y)
    page.mouse.down()
    page.mouse.move(bounds["x"] + 40, y, steps=6)
    page.mouse.up()
    position = canvas.evaluate("node => ({left: node.scrollLeft, scale: Number(node.dataset.graphScale)})")
    assert position["left"] > 20 and position["scale"] > 1
    page.locator(".daily-rfc > .page-heading").get_by_role("button", name="刷新", exact=True).click()
    expect(page.locator("#content")).not_to_have_class(re.compile(r"is-refreshing"))
    after = page.locator("#rfc-work .roadmap-track:visible .roadmap-canvas").first.evaluate("node => ({left: node.scrollLeft, scale: Number(node.dataset.graphScale)})")
    assert abs(position["left"] - after["left"]) <= 1 and position["scale"] == after["scale"]
    graph.get_by_role("button", name="适应画布", exact=True).click()
    assert canvas.evaluate("node => node.scrollLeft") == 0
    graph.get_by_role("button", name="重置", exact=True).click()
    assert canvas.get_attribute("data-graph-scale") == "1"
    page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#workspace-inspector .work-item[data-feature-id=F1]")).to_be_visible()
    assert not page.errors


def test_tabs_search_filters_and_english_keep_the_workspace(open_page):
    page = open_page()
    work = page.locator("#rfc-work")
    work.get_by_role("button", name="任务列表", exact=True).click()
    expect(work.locator(".task-row")).to_have_count(3)
    work.locator("input[name=task-search]").fill("F3")
    expect(work.locator(".task-row")).to_have_count(1)
    expect(work.locator(".task-row[data-feature-id=F3]")).to_be_visible()
    work.locator("input[name=task-search]").fill("")
    work.locator("select[name=task-track]").select_option("Engine")
    expect(work.locator(".task-row")).to_have_count(2)
    work.locator("select[name=task-owner]").select_option("Alice")
    expect(work.locator(".task-row")).to_have_count(1)
    for tab in ("goals", "design", "references", "work"):
        page.locator(f"[data-workspace-tab={tab}]").click()
        expect(page.locator(f"[data-workspace-tab={tab}]")).to_have_attribute("aria-selected", "true")
    expect(work.locator("select[name=task-track]")).to_have_value("Engine")
    page.locator(".language-switch [data-language=en]").click()
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#rfc-work select[name=task-track]")).to_have_value("Engine")
    assert not re.search(r"[\u4e00-\u9fff]", page.locator("[data-workspace-tab=work]").inner_text())
    assert not page.errors


def test_second_diagram_survives_settled_language_switch(open_page, site):
    page = open_page()
    diagram = page.locator("#rfc-work select[name=diagram]")
    options = diagram.locator("option").evaluate_all("nodes => nodes.filter(node => node.value !== '__all').map(node => ({value:node.value,label:node.textContent}))")
    assert len(options) >= 2 and options[1]["label"] == "工作依赖"
    stable_key = options[1]["value"]
    diagram.select_option(stable_key)
    expect(page.locator("#rfc-work .roadmap-track:visible")).to_have_count(1)
    expect(page.locator("#rfc-work .roadmap-track:visible .roadmap-actionable[data-feature-id=F3]")).to_be_visible()
    for language, title in (("en", "Work dependencies"), ("zh", "工作依赖")):
        with page.expect_response(lambda response: f"/api/v1/rfcs/{site.rfc_id}?" in response.url and f"language={language}" in response.url) as reading:
            page.locator(f".language-switch [data-language={language}]").click()
        reading.value.finished()
        expect(page.locator("#content")).not_to_have_class(re.compile(r"is-refreshing"))
        expect(diagram).to_have_value(stable_key)
        expect(diagram.locator("option:checked")).to_have_text(title)
        expect(page.locator("#rfc-work .roadmap-track:visible")).to_have_count(1)
        expect(page.locator("#rfc-work .roadmap-track:visible > .panel-title h3")).to_have_text(title)
        node = page.locator("#rfc-work .roadmap-track:visible .roadmap-actionable[data-feature-id=F3]")
        expect(node).to_be_visible()
        expect(node.locator("[data-graph-meta=state]")).to_contain_text("Acceptance" if language == "en" else "验收")
    page.locator(".daily-rfc > .page-heading").get_by_role("button", name="刷新", exact=True).click()
    expect(page.locator("#content")).not_to_have_class(re.compile(r"is-refreshing"))
    expect(diagram).to_have_value(stable_key)
    page.reload()
    expect(diagram).to_have_value(stable_key)
    expect(page.locator("#rfc-work .roadmap-track:visible .roadmap-actionable[data-feature-id=F3]")).to_be_visible()
    assert not page.errors


def test_hard_reload_restores_workspace_after_auth_and_prunes_revoked_selection(open_page, site):
    user = site.service.dispatch(site.owner, "users.create", {"name": "Workspace maintainer"})
    issued = site.service.dispatch(site.owner, "tokens.create", {"user_id": user["id"]})
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": "maintainer"})
    page = open_page(token=issued["token"])
    initial_revision = current(site)["revision"]
    for identity, modifier in (("F1", "Control"), ("F2", "Meta")):
        page.locator(f"#rfc-work .roadmap-actionable[data-feature-id={identity}]").first.click(modifiers=[modifier])
    page.locator("#rfc-work input[name=task-search]").fill("engine")
    for name, value in (("task-track", "Engine"), ("task-status", "planned"), ("task-owner", "Alice")):
        page.locator(f"#rfc-work select[name={name}]").select_option(value)
    graph = page.locator("#rfc-work .roadmap-track:visible").first
    canvas = graph.locator(".roadmap-canvas")
    for _ in range(6):
        graph.locator('button[aria-label$="放大"]').click()
    bounds = canvas.bounding_box()
    page.mouse.move(bounds["x"] + bounds["width"] - 40, bounds["y"] + 10)
    page.mouse.down()
    page.mouse.move(bounds["x"] + 40, bounds["y"] + 10, steps=6)
    page.mouse.up()
    position = canvas.evaluate("node => ({left:node.scrollLeft,scale:Number(node.dataset.graphScale)})")
    assert position["left"] > 20 and position["scale"] > 1
    page.reload()
    expect(page.locator(".daily-rfc")).to_be_visible()
    expect(page.locator("#rfc-work input[name=task-search]")).to_have_value("engine")
    for name, value in (("task-track", "Engine"), ("task-status", "planned"), ("task-owner", "Alice")):
        expect(page.locator(f"#rfc-work select[name={name}]")).to_have_value(value)
    page.wait_for_function("""position => {
        const canvas = document.querySelector('#rfc-work .roadmap-track:not([hidden]) .roadmap-canvas');
        return canvas && Math.abs(canvas.scrollLeft-position.left)<=1 && Number(canvas.dataset.graphScale)===position.scale;
    }""", arg=position)
    selected = page.locator("#rfc-work .roadmap-selected").evaluate_all("nodes => [...new Set(nodes.map(node=>node.dataset.featureId))].sort()")
    assert selected == ["F1", "F2"]
    assert current(site)["revision"] == initial_revision
    # Current permission is authoritative even when a previous checkpoint had a
    # writable selection. A later denied reload must purge it altogether.
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": "reader"})
    page.reload()
    expect(page.locator(".daily-rfc")).to_be_visible()
    expect(page.locator("#rfc-work .roadmap-track:visible svg").first).to_be_visible()
    expect(page.locator(".roadmap-selected")).to_have_count(0)
    expect(page.locator(".workspace-selection")).to_be_hidden()
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": ""})
    page.reload()
    expect(page.locator("#content .error-panel")).to_be_visible()
    assert page.evaluate("Object.keys(sessionStorage).filter(key=>key.startsWith('imrfc.workspace.v1:')).length") == 0
    assert not page.errors


def test_unsent_agent_input_survives_reload_and_clears_on_logout_and_access_loss(open_page, site):
    user = site.service.dispatch(site.owner, "users.create", {"name": "Chat maintainer"})
    issued = site.service.dispatch(site.owner, "tokens.create", {"user_id": user["id"]})
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": "maintainer"})
    page = open_page(token=issued["token"])
    first = "Private unsent Agent question, retained across reload"
    open_agent(page).fill(first)
    page.reload()
    expect(page.locator(".daily-rfc")).to_be_visible()
    expect(open_agent(page)).to_have_value(first)
    page.locator("#logout").click()
    expect(page.locator("#login")).to_be_visible()
    assert page.evaluate("Object.keys(sessionStorage).filter(key=>key.startsWith('imrfc.chat-input.v1:')).length") == 0
    assert first not in page.evaluate("JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))")
    page.locator("#login-token").fill(issued["token"])
    page.locator("#login-form button[type=submit]").click()
    expect(page.locator(".daily-rfc")).to_be_visible()
    expect(open_agent(page)).to_have_value("")
    second = "Private unsent Agent question, purged when RFC access ends"
    open_agent(page).fill(second)
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": ""})
    page.reload()
    expect(page.locator("#content .error-panel")).to_be_visible()
    assert second not in page.evaluate("JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))")
    expect(page.locator(".chat-composer textarea")).to_have_value("")
    assert not site.service.store.all("SELECT id FROM chat_jobs")
    assert not page.errors


def test_failed_chat_send_keeps_checkpoint_until_queue_acknowledges(open_page, site):
    # This sentinel enables the queue API without starting any model worker.
    site.service.chat.agent = object()
    page = open_page()
    text = "Keep this unsent message until the service acknowledges it"
    composer = open_agent(page)
    composer.fill(text)
    page.route("**/api/v1/actions/chat.send", lambda route: route.fulfill(status=503, json={
        "error": {"code": "transport_error", "message": "Temporary send failure"}}), times=1)
    send = page.locator("#workspace-agent .chat-composer").get_by_role("button", name="发送", exact=True)
    send.click()
    expect(page.locator("#workspace-agent .chat-error")).to_contain_text("Temporary send failure")
    expect(composer).to_have_value(text)
    assert text in page.evaluate("JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))")
    assert not site.service.store.all("SELECT id FROM chat_jobs")
    send.click()
    expect(composer).to_have_value("")
    assert text not in page.evaluate("JSON.stringify(Object.fromEntries(Object.entries(sessionStorage)))")
    assert [row["status"] for row in site.service.store.all("SELECT status FROM chat_jobs")] == ["pending"]
    assert not page.errors


def test_reader_can_inspect_but_cannot_mutate_or_select_for_merge(open_page, site):
    page = open_page(reader=True)
    before = current(site)["revision"]
    page.locator("#rfc-work .roadmap-actionable[data-feature-id=F1]").first.click(modifiers=["Control"])
    expect(page.locator("#workspace-inspector .work-item[data-feature-id=F1]")).to_be_visible()
    expect(page.locator(".roadmap-selected")).to_have_count(0)
    expect(page.locator("#workspace-inspector .item-actions")).to_have_count(0)
    expect(page.get_by_role("button", name="编辑 RFC", exact=True)).to_have_count(0)
    expect(page.locator(".workspace-selection")).to_be_hidden()
    assert current(site)["revision"] == before and not page.errors


def test_mobile_list_selection_and_fullscreen_inspector(open_page):
    page = open_page(mobile=True)
    work = page.locator("#rfc-work")
    work.get_by_role("button", name="任务列表", exact=True).click()
    work.locator(".task-row[data-feature-id=F1] input[data-select-feature]").check()
    work.locator(".task-row[data-feature-id=F2] input[data-select-feature]").check()
    expect(work.locator(".workspace-selection").get_by_role("button", name="合并选中节点", exact=True)).to_be_enabled()
    work.locator(".task-row[data-feature-id=F1] button.task-row-open").click()
    expect(page.locator("#workspace-inspector .work-item[data-feature-id=F1]")).to_be_visible()
    panel = page.locator(".workspace-side").bounding_box()
    assert panel and panel["x"] == 0 and panel["y"] == 0 and panel["width"] == 390
    page.locator(".workspace-side-tabs button.quiet").click()
    expect(page.locator(".workspace-side")).to_be_hidden()
    assert not page.errors


def test_manual_source_save_requires_review_and_recovers_without_losing_input(open_page, site):
    page = open_page()
    page.get_by_role("button", name="编辑 RFC", exact=True).click()
    editor = page.locator("dialog.rfc-editor[open]")
    changed = BODY + "\nReviewed browser update.\n"
    editor.locator("textarea[name=body]").fill(changed)
    editor.locator("input[name=reason]").fill("Clarify the proposed design")
    editor.get_by_role("button", name="保存新版本", exact=True).click()
    review = page.locator("dialog.wide-review[open]")
    expect(review).to_be_visible()
    expect(review.locator(".review-diff").first).to_contain_text("Reviewed browser update")
    assert current(site)["body"] == BODY and site.source.read_text(encoding="utf-8") == BODY
    page.route("**/api/v1/actions/rfcs.changes.apply", lambda route: route.fulfill(status=503, json={
        "error": {"code": "transport_error", "message": "Temporary connection failure; retry the reviewed candidate"}}), times=1)
    review.get_by_role("button", name="应用并同步来源", exact=True).click()
    expect(review.locator(".form-error")).to_contain_text("Temporary connection failure")
    expect(editor.locator("textarea[name=body]")).to_have_value(changed)
    assert current(site)["body"] == BODY
    review.get_by_role("button", name="应用并同步来源", exact=True).click()
    expect(review).not_to_be_visible()
    expect(editor).not_to_be_visible()
    expect(page.locator("#rfc-source-update")).to_contain_text("等待同步")
    assert current(site)["body"] == changed and site.source.read_text(encoding="utf-8") == BODY
    assert site.service.process_pending()[0]["status"] == "succeeded"
    page.reload()
    expect(page.locator("#rfc-source-update")).to_contain_text("已同步来源")
    assert site.source.read_text(encoding="utf-8") == changed
    assert len(site.service.store.all("SELECT id FROM operations WHERE kind='rfcs.update_source'")) == 1
    assert not page.errors


def test_shipped_english_catalog_works_when_translation_service_is_unavailable(open_page):
    page = open_page(language="en", offline_catalog=True)
    expect(page.locator("html")).to_have_attribute("lang", "en")
    for selector in ("[data-workspace-tab=work]", "[data-workspace-tab=goals]", ".workspace-open-details"):
        assert not re.search(r"[\u4e00-\u9fff]", page.locator(selector).inner_text())
    expect(page.get_by_role("button", name="Edit RFC", exact=True)).to_be_visible()
    inspect(page, "F1")
    assert not page.errors


def test_dirty_editor_survives_refresh_language_reload_and_clears_on_logout(open_page, site):
    page = open_page()
    changed = BODY + "\nPrivate unfinished design.\n"
    page.get_by_role("button", name="编辑 RFC", exact=True).click()
    editor = page.locator("dialog.rfc-editor[open]")
    editor.locator("textarea[name=body]").fill(changed)
    editor.locator("input[name=reason]").fill("Unfinished reason")
    editor.get_by_role("button", name="关闭编辑器", exact=True).click()
    page.locator(".daily-rfc > .page-heading").get_by_role("button", name="刷新", exact=True).click()
    expect(page.locator(".daily-rfc")).to_be_visible()
    page.get_by_role("button", name="编辑 RFC", exact=True).click()
    expect(page.locator("dialog.rfc-editor[open] textarea[name=body]")).to_have_value(changed)
    page.locator("dialog.rfc-editor[open]").get_by_role("button", name="关闭编辑器", exact=True).click()
    page.locator(".language-switch [data-language=en]").click()
    expect(page.get_by_role("button", name="Edit RFC", exact=True)).to_be_visible()
    page.get_by_role("button", name="Edit RFC", exact=True).click()
    expect(page.locator("dialog.rfc-editor[open] textarea[name=body]")).to_have_value(changed)
    page.reload()
    expect(page.locator(".daily-rfc")).to_be_visible()
    page.get_by_role("button", name="Edit RFC", exact=True).click()
    expect(page.locator("dialog.rfc-editor[open] textarea[name=body]")).to_have_value(changed)
    expect(page.locator("dialog.rfc-editor[open] input[name=reason]")).to_have_value("Unfinished reason")
    storage = page.evaluate("JSON.stringify({session: Object.fromEntries(Object.entries(sessionStorage)), local: Object.fromEntries(Object.entries(localStorage))})")
    assert site.token not in storage and "Private unfinished design" in storage
    page.locator("dialog.rfc-editor[open]").get_by_role("button", name="Close editor", exact=True).click()
    page.locator("#logout").click()
    expect(page.locator("#login")).to_be_visible()
    assert page.evaluate("Object.keys(sessionStorage).filter(key => key.startsWith('imrfc.draft.v2:')).length") == 0
    expect(page.locator("dialog.rfc-editor")).to_have_count(0)
    assert current(site)["body"] == BODY and not page.errors


def test_access_loss_clears_private_checkpoint_and_editor(open_page, site):
    user = site.service.dispatch(site.owner, "users.create", {"name": "Temporary maintainer"})
    issued = site.service.dispatch(site.owner, "tokens.create", {"user_id": user["id"]})
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": "maintainer"})
    page = open_page(token=issued["token"])
    page.get_by_role("button", name="编辑 RFC", exact=True).click()
    page.locator("dialog.rfc-editor[open] textarea[name=body]").fill(BODY + "Private revoked draft")
    page.locator("dialog.rfc-editor[open]").get_by_role("button", name="关闭编辑器", exact=True).click()
    site.service.dispatch(site.owner, "grants.set", {"repo_id": site.repository["id"], "user_id": user["id"], "role": ""})
    page.locator(".daily-rfc > .page-heading").get_by_role("button", name="刷新", exact=True).click()
    expect(page.locator("#content .error-panel")).to_be_visible()
    assert page.evaluate("Object.keys(sessionStorage).filter(key => key.startsWith('imrfc.draft.v2:')).length") == 0
    expect(page.locator("dialog.rfc-editor")).to_have_count(0)
    assert not page.errors
