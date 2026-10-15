/* Private tab checkpoints. Authorization comes from the service, never storage. */
const PREFIX = "imrfc.draft.v2:";
export function createDraftKeeper({scope}) {
  const keyFor = form => {
    const current = scope();
    if (!current?.user_id || form.matches("[data-no-draft]") || form.closest("[data-no-draft]") || form.querySelector('input[type="password"],#issued-secret')) return null;
    const identity = form.dataset.draftKey;
    if (!identity) return null;
    if (!form.dataset.draftScope) form.dataset.draftScope = JSON.stringify([current.user_id, current.repo_id || "", current.rfc_id || ""]);
    const [user, repo, rfc] = JSON.parse(form.dataset.draftScope);
    if (user !== current.user_id) return null;
    return PREFIX + JSON.stringify([user, repo, rfc, identity]);
  };
  const fields = form => [...form.querySelectorAll("input[name],textarea[name],select[name]")].filter(input => !input.disabled && !["password", "file", "hidden"].includes(input.type) && !/token|secret|credential/i.test(input.name));
  const write = form => {
    if (form.dataset.draftSaved === "true" || form.dataset.draftDirty !== "true") return;
    const key = keyFor(form); if (!key) return;
    const values = fields(form).map(input => ({name: input.name, type: input.type, value: ["checkbox", "radio"].includes(input.type) ? input.checked : input.value}));
    try { sessionStorage.setItem(key, JSON.stringify(values)); } catch { /* Quota/private mode cannot prevent editing. */ }
  };
  const forms = root => [...(root.matches?.("form") ? [root] : []), ...root.querySelectorAll("form[data-draft-key]")];
  const capture = root => forms(root).forEach(write);
  const restore = root => {
    for (const form of forms(root)) {
      const key = keyFor(form); if (!key) continue;
      let data; try { data = JSON.parse(sessionStorage.getItem(key) || "null"); } catch { continue; }
      if (!Array.isArray(data)) continue;
      const controls = fields(form);
      for (const item of data) {
        const input = controls.find(input => input.name === item.name && input.type === item.type);
        if (!input) continue;
        if (["checkbox", "radio"].includes(input.type)) input.checked = Boolean(item.value);
        else if (typeof item.value === "string") input.value = item.value;
      }
      form.dataset.draftRestored = "true";
      form.dataset.draftDirty = "true";
      form.dispatchEvent(new Event("input", {bubbles: true}));
    }
  };
  const clearScope = () => {
    const current = scope(); if (!current?.user_id) return;
    try {
      for (let index = sessionStorage.length - 1; index >= 0; index--) {
        const key = sessionStorage.key(index); if (!key?.startsWith(PREFIX)) continue;
        const [user, repo, rfc] = JSON.parse(key.slice(PREFIX.length));
        if (user === current.user_id && repo === (current.repo_id || "") && rfc === (current.rfc_id || "")) sessionStorage.removeItem(key);
      }
    } catch {}
  };
  const clear = () => {
    try { for (let index = sessionStorage.length - 1; index >= 0; index--) { const key = sessionStorage.key(index); if (key?.startsWith(PREFIX)) sessionStorage.removeItem(key); } } catch {}
  };
  const forget = root => { for (const form of forms(root)) { const key = keyFor(form); form.dataset.draftSaved = "true"; delete form.dataset.draftDirty; if (key) try { sessionStorage.removeItem(key); } catch {} } };
  const input = event => { const form = event.target.closest?.("form[data-draft-key]"); if (form) { delete form.dataset.draftSaved; form.dataset.draftDirty = "true"; write(form); } };
  document.addEventListener("input", input); document.addEventListener("change", input);
  document.addEventListener("draft-saved", event => { const form = event.target.closest?.("form"); if (form) forget(form); });
  window.addEventListener("pagehide", () => capture(document));
  return {capture, restore, clear, clearScope, forget};
}
