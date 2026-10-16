/* Public language preference only; translated RFC text stays in session memory. */
import {englishUI} from "/roadmap-ui-catalog.mjs";
let language = new URL(location.href).searchParams.get("lang");
try { language ||= localStorage.getItem("imrfc.language"); } catch {}
language = language === "en" ? "en" : "zh";
let interfaceStrings = language === "en" ? {...englishUI} : {}, privateStrings = {}, templates = [], version = 0;
const originals = new WeakMap(), memo = new Map();
export const currentLanguage = () => language;
const escape = value => value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");

function reset() {
  version += 1; memo.clear();
  templates = Object.entries(interfaceStrings).filter(([source]) => /⟪S\d+⟫/.test(source)).map(([source, translated]) => {
    const slots = [], parts = source.split(/(⟪S\d+⟫)/);
    return {pattern: new RegExp("^" + parts.map(part => {
      if (/^⟪S\d+⟫$/.test(part)) { slots.push(part); return "([\\s\\S]*?)"; }
      return escape(part);
    }).join("") + "$"), slots, translated};
  });
}

export function localizeText(value) {
  if (typeof value !== "string" || !value.trim()) return value;
  if (memo.has(value)) return memo.get(value);
  const key = value.trim();
  let result = privateStrings[key] || interfaceStrings[key];
  if (!result && language === "en") {
    for (const template of templates) {
      const match = key.match(template.pattern);
      if (!match) continue;
      result = template.translated;
      template.slots.forEach((slot, index) => { result = result.split(slot).join(localizeText(match[index + 1])); });
      break;
    }
  }
  const answer = result ? value.slice(0, value.indexOf(key)) + result + value.slice(value.indexOf(key) + key.length) : value;
  memo.set(value, answer);
  return answer;
}

const sameStrings = (left, right) => Object.keys(left).length === Object.keys(right).length && Object.entries(right).every(([key, value]) => left[key] === value);
export function setTranslations(snapshot, replace = true) {
  const next = replace ? {} : {...privateStrings};
  if (snapshot?.language === language) Object.assign(next, snapshot.strings || {});
  if (sameStrings(privateStrings, next)) return;
  privateStrings = next;
  reset(); applyLocale(document.body);
}

export function applyLocale(root) {
  if (!root) return;
  const untouched = "svg,code,pre,[data-no-translate],#identity-name,#issued-secret";
  if ((root.nodeType === Node.ELEMENT_NODE ? root : root.parentElement)?.closest(untouched)) return;
  const visit = node => {
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.parentElement?.closest("svg,code,pre,textarea,input,[data-no-translate],#identity-name,#issued-secret")) return;
      let state = originals.get(node);
      if (!state || node.nodeValue !== state.applied) state = {source: node.nodeValue};
      state.applied = localizeText(state.source);
      originals.set(node, state);
      if (node.nodeValue !== state.applied) node.nodeValue = state.applied;
    } else if (node.nodeType === Node.ELEMENT_NODE && !node.closest("svg,[data-no-translate]")) {
      let state = originals.get(node) || {};
      for (const name of ["placeholder", "aria-label", "title"]) {
        const value = node.getAttribute(name);
        if (value === null) continue;
        if (!state[name] || value !== state[name].applied) state[name] = {source: value};
        state[name].applied = localizeText(state[name].source);
        if (value !== state[name].applied) node.setAttribute(name, state[name].applied);
      }
      originals.set(node, state);
    }
  };
  visit(root);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT, {acceptNode: node => node.nodeType === Node.ELEMENT_NODE && node.matches(untouched) ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT});
  while (walker.nextNode()) visit(walker.currentNode);
}

let catalogRequest = null;
export async function initializeLocale() {
  const requestedLanguage = language;
  document.documentElement.lang = language === "zh" ? "zh-CN" : "en";
  applyLocale(document.body);
  document.querySelectorAll("[data-language]").forEach(button => button.setAttribute("aria-pressed", String(button.dataset.language === language)));
  // The shipped catalog is usable immediately; a failed translation service
  // must never delay authentication or navigation.
  if (catalogRequest?.language === requestedLanguage) return;
  const request = {language: requestedLanguage}; catalogRequest = request;
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 5000);
  fetch(`/roadmap-languages.json?language=${requestedLanguage}`, {cache: "no-store", signal: controller.signal}).then(async response => {
    if (!response.ok || language !== requestedLanguage) return;
    const next = {...(requestedLanguage === "en" ? englishUI : {}), ...((await response.json()).strings || {})};
    if (language === requestedLanguage && !sameStrings(interfaceStrings, next)) { interfaceStrings = next; reset(); applyLocale(document.body); }
  }).catch(() => {}).finally(() => { clearTimeout(timer); if (catalogRequest === request) catalogRequest = null; });
}

export async function changeLanguage(next) {
  language = next === "en" ? "en" : "zh";
  interfaceStrings = language === "en" ? {...englishUI} : {}; privateStrings = {}; reset();
  try { localStorage.setItem("imrfc.language", language); } catch {}
  const url = new URL(location.href); url.searchParams.set("lang", language); history.replaceState(null, "", url);
  await initializeLocale();
}

const observer = new MutationObserver(records => {
  const roots = new Set();
  for (const record of records) {
    if (record.type === "childList") for (const node of record.addedNodes) roots.add(node);
    else roots.add(record.target);
  }
  for (const root of roots) applyLocale(root);
});
observer.observe(document.body, {subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ["placeholder", "aria-label", "title"]});
reset();
setInterval(() => initializeLocale().catch(() => {}), 30000);
