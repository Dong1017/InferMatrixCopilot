import {applyLocale, localizeText} from "/roadmap-locale.mjs";

const make = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (value !== undefined && value !== null && value !== false) node.setAttribute(key, String(value));
  }
  for (const child of children.flat()) if (child !== null && child !== undefined) node.append(child instanceof Node ? child : String(child));
  return node;
};

/** An action only dismisses its form once the server confirms success. */
export function actionDialog({title, description = "", fields = [], submitLabel = "确认", onSubmit, extra = null, className = "", draftKey = ""}) {
  return new Promise(resolve => {
    const previous = document.activeElement;
    const target = previous?.closest("[data-feature-id],[data-criterion-id],[data-graph-node-id]");
    const contextKey = target?.dataset.featureId || target?.dataset.criterionId || target?.dataset.graphNodeId || "";
    const checkpoint = draftKey || (fields.length ? `action:${title}:${contextKey}:${fields.map(field => field.name).join(",")}` : "");
    const dialog = make("dialog", {class: `action-dialog ${className}`, "aria-label": title});
    const form = make("form", {class: "stack", ...(checkpoint ? {"data-draft-key": checkpoint} : {})});
    const inputs = new Map();
    const error = make("p", {class: "form-error", role: "alert", hidden: true});
    const submit = make("button", {type: "submit", class: "primary"}, submitLabel);
    let pending = false, applied = false;
    const cancel = make("button", {type: "button", class: "quiet", onclick: () => { if (!pending) dialog.close(); }}, "取消");
    form.append(make("h2", {}, title));
    if (description) form.append(make("p", {class: "muted"}, description));
    if (extra) form.append(extra);
    for (const field of fields) {
      const id = `action-field-${actionDialog.serial++}`;
      const control = make(field.type === "textarea" ? "textarea" : field.type === "select" ? "select" : "input", {
        name: field.name, id, required: field.required, ...(field.type === "textarea" ? {rows: field.rows || 4} : field.type !== "select" ? {type: field.type || "text"} : {}),
        ...(field.placeholder ? {placeholder: field.placeholder} : {})});
      if (field.type === "select") for (const choice of field.options || []) {
        const [value, label] = Array.isArray(choice) ? choice : [choice, choice];
        control.append(make("option", {value}, label));
      }
      control.value = field.value ?? "";
      inputs.set(field.name, control);
      form.append(make("div", {class: "field"}, make("label", {for: id}, field.label || field.name), control,
        field.help ? make("p", {class: "help"}, field.help) : null));
    }
    form.append(error, make("div", {class: "actions"}, submit, cancel));
    form.addEventListener("submit", async event => {
      event.preventDefault();
      if (pending || !form.reportValidity()) return;
      const values = Object.fromEntries([...inputs].map(([name, input]) => [name, input.value.trim()]));
      const empty = fields.find(field => field.required && !values[field.name]);
      if (empty) { inputs.get(empty.name).focus(); return; }
      pending = true; submit.disabled = cancel.disabled = true; error.hidden = true;
      dialog.setAttribute("aria-busy", "true"); submit.textContent = localizeText("处理中…");
      try {
        await onSubmit?.(values);
        applied = true; form.dispatchEvent(new CustomEvent("draft-saved", {bubbles: true})); dialog.close();
      } catch (failure) {
        error.textContent = failure.message || localizeText("操作未完成，请重试。"); error.hidden = false; error.focus();
      } finally {
        pending = false; submit.disabled = cancel.disabled = false; dialog.removeAttribute("aria-busy"); submit.textContent = localizeText(submitLabel);
      }
    });
    dialog.addEventListener("cancel", event => { if (pending) event.preventDefault(); });
    dialog.addEventListener("close", () => { dialog.remove(); if (previous?.isConnected) previous.focus({preventScroll: true}); resolve(applied); }, {once: true});
    dialog.append(form); document.body.append(dialog); form.dispatchEvent(new CustomEvent("draft-form-opened", {bubbles: true})); applyLocale(dialog); dialog.showModal();
    (inputs.values().next().value || submit).focus();
  });
}
actionDialog.serial = 0;

export async function askText(title, initialValue = "") {
  let value = null;
  await actionDialog({title, fields: [{name: "value", label: title, value: initialValue, required: true}], onSubmit: fields => { value = fields.value; }});
  return value;
}
export function askConfirm(message) { return actionDialog({title: "确认操作", description: message, submitLabel: "确认继续"}); }

/** Render source as text, with readable differences and no HTML execution. */
export function renderDiff(text) {
  const result = make("div", {class: "review-diff", "data-no-translate": "", tabindex: "0", role: "region", "aria-label": "Source diff"});
  for (const line of String(text || "").split("\n")) result.append(make("div", {class: line.startsWith("+") && !line.startsWith("+++") ? "diff-added" : line.startsWith("-") && !line.startsWith("---") ? "diff-removed" : line.startsWith("@@") ? "diff-range" : "diff-context"}, line || " "));
  return result;
}
