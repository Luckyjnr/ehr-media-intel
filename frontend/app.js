// EHR Media Intelligence: clinician search page.
//
// Plain JavaScript, no framework. Calls the same-origin API:
//   POST /search          -> result cards
//   GET  /patients/{mrn}  -> patient drawer
//
// Safety rule: data from the API is only ever put on the page with
// textContent (never innerHTML), so record text cannot inject HTML or scripts.

"use strict";

const SEARCH_DELAY_MS = 300; // wait this long after typing stops before searching
const EXAMPLE_SEARCHES = ["patients with high blood sugar", "brain scan for memory problems"];

const TYPE_LABELS = {
  DocumentReference: "Document",
  DiagnosticReport: "Lab report",
  Summary: "AI summary",
};
const TYPE_BADGE_CLASSES = {
  DocumentReference: "bg-badge-doc text-badge-doc-text",
  DiagnosticReport: "bg-badge-lab text-badge-lab-text",
  Summary: "bg-badge-summary text-badge-summary-text",
};
const CONFIDENCE_CLASSES = {
  high: "bg-mint text-deep",
  medium: "bg-warning text-warning-text",
  low: "bg-critical text-critical-text",
};

// ---------------------------------------------------------------------------
// Page elements
// ---------------------------------------------------------------------------

const form = document.getElementById("search-form");
const searchInput = document.getElementById("search-input");
const typeFilter = document.getElementById("filter-type");
const dateFromFilter = document.getElementById("filter-from");
const dateToFilter = document.getElementById("filter-to");
const resultsSection = document.getElementById("results");
const resultsStatus = document.getElementById("results-status");
const resultsBody = document.getElementById("results-body");

const pageHeader = document.getElementById("page-header");
const pageMain = document.getElementById("page-main");
const drawerRoot = document.getElementById("drawer-root");
const drawerBackdrop = document.getElementById("drawer-backdrop");
const drawer = document.getElementById("drawer");
const drawerTitle = document.getElementById("drawer-title");
const drawerMrn = document.getElementById("drawer-mrn");
const drawerBody = document.getElementById("drawer-body");
const drawerClose = document.getElementById("drawer-close");

// Each search gets a number. When a reply arrives, we only show it if its
// number is still the latest, so a slow old reply can never replace newer results.
let latestSearchId = 0;
let latestPatientRequestId = 0;
let searchTimer = null;
let cardThatOpenedDrawer = null;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

/** Create an element with classes and (safe) text. */
function el(tag, className = "", text = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== null && text !== undefined) node.textContent = String(text);
  return node;
}

function clear(node) {
  node.replaceChildren();
}

/** Turn a FastAPI error reply into one readable sentence. */
async function readErrorMessage(response) {
  try {
    const body = await response.json();
    if (typeof body.detail === "string") return body.detail;
    if (Array.isArray(body.detail)) {
      // 422 validation errors: [{loc: ["query", "date_from"], msg: "..."}]
      return body.detail
        .map((problem) => `${problem.loc[problem.loc.length - 1]}: ${problem.msg.replace(/^Value error, /, "")}`)
        .join(". ");
    }
  } catch (error) {
    // Not JSON: fall through to the generic message.
  }
  return `The server replied with status ${response.status}.`;
}

function typeBadge(resourceType) {
  return el(
    "span",
    `inline-flex rounded-full px-2.5 py-0.5 text-xs font-bold ${TYPE_BADGE_CLASSES[resourceType] || "bg-mint text-deep"}`,
    TYPE_LABELS[resourceType] || resourceType,
  );
}

function messageBox(kind, title, detail) {
  // kind: "error" (red) or "info" (neutral)
  const isError = kind === "error";
  const box = el(
    "div",
    isError
      ? "rounded-card border border-critical-border bg-critical p-5 text-critical-text"
      : "rounded-card border border-line bg-card p-5 shadow-card",
  );
  if (isError) box.setAttribute("role", "alert");
  box.append(el("p", "font-bold", title));
  if (detail) box.append(el("p", `mt-1 text-sm ${isError ? "" : "text-muted"}`, detail));
  return box;
}

// ---------------------------------------------------------------------------
// Search states: hint, loading, results, empty, error
// ---------------------------------------------------------------------------

function showHint() {
  resultsStatus.textContent = "";
  const box = el("div", "rounded-card border border-line bg-card p-5 shadow-card");
  box.append(el("p", "font-bold", "Search by meaning, not exact words."));
  box.append(el("p", "mt-1 text-sm text-muted", "Try one of these:"));
  const examples = el("div", "mt-3 flex flex-wrap gap-2");
  for (const example of EXAMPLE_SEARCHES) {
    const button = el(
      "button",
      "rounded-full border border-field px-4 py-2 text-sm font-bold text-brand-dark transition hover:bg-mint",
      example,
    );
    button.type = "button";
    button.addEventListener("click", () => {
      searchInput.value = example;
      runSearch();
    });
    examples.append(button);
  }
  box.append(examples);
  resultsBody.replaceChildren(box);
}

function showLoading() {
  resultsSection.setAttribute("aria-busy", "true");
  resultsStatus.textContent = "Searching…";
  const list = el("div", "grid gap-3");
  for (let index = 0; index < 3; index += 1) {
    const skeleton = el("div", "animate-pulse rounded-card border border-line bg-card p-5 shadow-card");
    skeleton.setAttribute("aria-hidden", "true");
    skeleton.append(el("div", "h-4 w-1/3 rounded-full bg-line"));
    skeleton.append(el("div", "mt-3 h-3 w-full rounded-full bg-mint"));
    skeleton.append(el("div", "mt-2 h-3 w-2/3 rounded-full bg-mint"));
    list.append(skeleton);
  }
  resultsBody.replaceChildren(list);
}

function showError(title, detail) {
  resultsStatus.textContent = "";
  resultsBody.replaceChildren(messageBox("error", title, detail));
}

function renderResults(data) {
  const results = data.results;
  const count = results.length;
  resultsStatus.textContent = `${count} ${count === 1 ? "result" : "results"} in ${Math.round(data.took_ms)} ms`;

  if (count === 0) {
    resultsBody.replaceChildren(
      messageBox("info", "No matching records.", "Try different words, or remove some filters (type or dates)."),
    );
    return;
  }

  const list = el("ul", "grid gap-3");
  list.setAttribute("aria-label", "Search results");
  // The API already sorts by score; we sort again so the order never depends on it.
  const sorted = [...results].sort((a, b) => b.score - a.score);
  for (const result of sorted) {
    const item = el("li");
    item.append(resultCard(result));
    list.append(item);
  }
  resultsBody.replaceChildren(list);
}

/** One result card. A real <button>, so Tab and Enter work without extra code. */
function resultCard(result) {
  const card = el(
    "button",
    "group block w-full rounded-card border border-line bg-card p-4 text-left shadow-card transition " +
      "hover:-translate-y-0.5 hover:border-brand hover:shadow-card-hover sm:p-5",
  );
  card.type = "button";
  card.dataset.mrn = result.mrn;

  const top = el("div", "flex flex-wrap items-center gap-x-3 gap-y-1");
  top.append(el("span", "text-base font-bold", result.patient_name));
  top.append(el("span", "text-sm font-semibold text-muted tabular-nums", `MRN ${result.mrn}`));
  top.append(typeBadge(result.resource_type));
  card.append(top);

  // Date on the left, score on the right: stays on one line even on phones.
  const meta = el("div", "mt-1 flex items-baseline justify-between gap-3");
  meta.append(el("p", "text-sm font-medium text-muted tabular-nums", result.date || "No date"));
  meta.append(el("span", "shrink-0 text-sm font-bold text-brand-dark tabular-nums", `Score ${result.score.toFixed(2)}`));
  card.append(meta);

  // What matched, so cards for the same patient can be told apart.
  card.append(el("p", "mt-2 text-sm text-ink line-clamp-2", result.snippet));

  if (result.summary_snippet) {
    const summary = el("div", "mt-3 rounded-xl bg-surface px-3 py-2");
    summary.append(el("p", "text-[11px] font-bold uppercase tracking-[0.08em] text-brand-dark", "AI summary"));
    summary.append(el("p", "mt-0.5 text-sm text-ink line-clamp-3", result.summary_snippet));
    card.append(summary);
  }

  // Accessible name: the most useful facts first, instead of the whole card text.
  card.setAttribute(
    "aria-label",
    `${result.patient_name}, MRN ${result.mrn}, ${TYPE_LABELS[result.resource_type] || result.resource_type}, ` +
      `${result.date || "no date"}, score ${result.score.toFixed(2)}. Open patient details.`,
  );
  card.addEventListener("click", () => openDrawer(result.mrn, card));
  return card;
}

// ---------------------------------------------------------------------------
// Running a search
// ---------------------------------------------------------------------------

function buildSearchUrl() {
  const params = new URLSearchParams();
  if (typeFilter.value) params.set("resource_type", typeFilter.value);
  if (dateFromFilter.value) params.set("date_from", dateFromFilter.value);
  if (dateToFilter.value) params.set("date_to", dateToFilter.value);
  const query = params.toString();
  return query ? `/search?${query}` : "/search";
}

async function runSearch() {
  clearTimeout(searchTimer);
  const query = searchInput.value.trim();
  const searchId = ++latestSearchId;

  if (!query) {
    resultsSection.setAttribute("aria-busy", "false");
    showHint();
    return;
  }

  showLoading();
  try {
    const response = await fetch(buildSearchUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ query }),
    });
    if (searchId !== latestSearchId) return; // a newer search has started; drop this reply

    if (!response.ok) {
      const message = await readErrorMessage(response);
      if (searchId !== latestSearchId) return;
      const title = response.status === 422 ? "Please check your search or dates." : "The search failed.";
      showError(title, message);
      return;
    }

    const data = await response.json();
    if (searchId !== latestSearchId) return;
    renderResults(data);
  } catch (error) {
    if (searchId !== latestSearchId) return;
    showError("Cannot reach the search service.", "Check that the API is running, then try again.");
  } finally {
    if (searchId === latestSearchId) resultsSection.setAttribute("aria-busy", "false");
  }
}

function scheduleSearch() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, SEARCH_DELAY_MS);
}

form.addEventListener("submit", (event) => {
  event.preventDefault(); // Enter searches now, without reloading the page
  runSearch();
});
searchInput.addEventListener("input", scheduleSearch);
// Changing a filter re-runs the current search straight away.
for (const filter of [typeFilter, dateFromFilter, dateToFilter]) {
  filter.addEventListener("change", runSearch);
}

// ---------------------------------------------------------------------------
// Patient drawer
// ---------------------------------------------------------------------------

function section(title) {
  const wrapper = el("section", "mt-6");
  wrapper.append(el("h3", "text-[11px] font-bold uppercase tracking-[0.12em] text-brand-dark", title));
  return wrapper;
}

function bulletList(items, emptyText) {
  if (!items || items.length === 0) return el("p", "mt-2 text-sm text-muted", emptyText);
  const list = el("ul", "mt-2 list-disc space-y-1 pl-5 text-sm");
  for (const item of items) list.append(el("li", "", item));
  return list;
}

function anomalyList(anomalies) {
  if (!anomalies || anomalies.length === 0) {
    return el("p", "mt-2 text-sm text-muted", "No flagged lab results.");
  }
  const list = el("ul", "mt-2 grid gap-2");
  for (const anomaly of anomalies) {
    // Critical values in red, high/low in amber. The word in the text tells us which.
    const critical = /critical/i.test(anomaly);
    const item = el(
      "li",
      critical
        ? "rounded-xl border border-critical-border bg-critical px-3 py-2 text-sm font-semibold text-critical-text tabular-nums"
        : "rounded-xl border border-warning-border bg-warning px-3 py-2 text-sm font-semibold text-warning-text tabular-nums",
    );
    // A text label too, so the meaning does not rely on colour alone.
    item.append(el("span", "mr-1 font-bold", critical ? "Critical:" : "Flagged:"));
    item.append(document.createTextNode(anomaly));
    list.append(item);
  }
  return list;
}

function renderPatient(patient) {
  drawerTitle.textContent = patient.patient_name;
  drawerMrn.textContent = `MRN ${patient.mrn}`;
  const parts = [];
  const summary = patient.summary;

  if (!summary) {
    parts.push(messageBox("info", "No AI summary yet.", "Run the summaries step (python -m app.summaries.build)."));
  } else {
    // Disclaimer first and clearly visible.
    const disclaimer = el("div", "rounded-card border border-accent bg-accent-soft p-4");
    disclaimer.setAttribute("role", "note");
    disclaimer.append(el("p", "text-sm font-bold text-ink", "AI-generated summary"));
    disclaimer.append(el("p", "mt-1 text-sm text-ink", summary.disclaimer));
    parts.push(disclaimer);

    const confidence = section("Confidence");
    const confidenceRow = el("div", "mt-2 flex flex-wrap items-center gap-2");
    confidenceRow.append(
      el(
        "span",
        `rounded-full px-3 py-1 text-xs font-bold uppercase tracking-[0.08em] ${CONFIDENCE_CLASSES[summary.confidence] || "bg-mint text-deep"}`,
        summary.confidence,
      ),
    );
    confidence.append(confidenceRow);
    confidence.append(el("p", "mt-2 text-sm text-muted", summary.confidence_reason));
    parts.push(confidence);

    const concern = section("Chief concern");
    concern.append(el("p", "mt-2 text-sm", summary.chief_concern));
    parts.push(concern);

    const diagnoses = section("Key diagnoses");
    diagnoses.append(bulletList(summary.key_diagnoses, "Not documented."));
    parts.push(diagnoses);

    const anomalies = section("Flagged anomalies");
    anomalies.append(anomalyList(summary.flagged_anomalies));
    parts.push(anomalies);

    const media = section("Recent media");
    if (summary.recent_media.length === 0) {
      media.append(el("p", "mt-2 text-sm text-muted", "No documents."));
    } else {
      const list = el("ul", "mt-2 grid gap-2");
      for (const item of summary.recent_media) {
        const row = el("li", "rounded-xl border border-line bg-card px-3 py-2");
        const heading = el("p", "flex flex-wrap gap-x-2 text-sm font-bold");
        heading.append(el("span", "", item.type));
        heading.append(el("span", "font-semibold text-muted tabular-nums", item.date));
        row.append(heading);
        row.append(el("p", "mt-0.5 text-sm", item.note));
        list.append(row);
      }
      media.append(list);
    }
    parts.push(media);

    const text = section("Summary");
    text.append(el("p", "mt-2 text-sm leading-relaxed", summary.summary_text));
    text.append(el("p", "mt-2 text-xs text-muted", `${summary.model}, prompt ${summary.prompt_version}`));
    parts.push(text);
  }

  const resources = section("Linked FHIR resources");
  const table = el("ul", "mt-2 divide-y divide-line rounded-xl border border-line bg-card");
  for (const resource of patient.resources) {
    const row = el("li", "px-3 py-2 text-sm");
    const heading = el("p", "flex flex-wrap items-center gap-x-2");
    heading.append(el("span", "font-bold", resource.type));
    heading.append(el("span", "font-semibold text-muted tabular-nums", resource.date || "No date"));
    row.append(heading);
    row.append(el("p", "mt-0.5 tabular-nums", resource.description));
    table.append(row);
  }
  resources.append(table);
  parts.push(resources);

  drawerBody.replaceChildren(...parts);
}

async function loadPatient(mrn) {
  const requestId = ++latestPatientRequestId;
  drawerBody.setAttribute("aria-busy", "true");
  drawerBody.replaceChildren(el("p", "text-sm font-semibold text-muted", "Loading patient record…"));

  try {
    const response = await fetch(`/patients/${encodeURIComponent(mrn)}`);
    if (requestId !== latestPatientRequestId) return;
    if (!response.ok) {
      const message = await readErrorMessage(response);
      if (requestId !== latestPatientRequestId) return;
      const title =
        response.status === 404
          ? "Record not available. It may be held for review."
          : "Could not load this patient.";
      drawerBody.replaceChildren(messageBox("error", title, message));
      return;
    }
    const patient = await response.json();
    if (requestId !== latestPatientRequestId) return;
    renderPatient(patient);
  } catch (error) {
    if (requestId !== latestPatientRequestId) return;
    drawerBody.replaceChildren(
      messageBox("error", "Cannot reach the API.", "Check that the server is running, then try again."),
    );
  } finally {
    if (requestId === latestPatientRequestId) drawerBody.setAttribute("aria-busy", "false");
  }
}

function openDrawer(mrn, openerCard) {
  cardThatOpenedDrawer = openerCard;
  drawerTitle.textContent = "Patient";
  drawerMrn.textContent = `MRN ${mrn}`;

  drawerRoot.hidden = false;
  // Everything behind the drawer becomes unreachable by Tab and screen readers.
  pageHeader.inert = true;
  pageMain.inert = true;
  document.body.classList.add("overflow-hidden");

  // Next frame, so the slide-in transition runs from the hidden position.
  requestAnimationFrame(() => {
    drawer.classList.remove("translate-x-full");
    drawerBackdrop.classList.remove("opacity-0");
  });
  drawerTitle.focus();
  loadPatient(mrn);
}

function closeDrawer() {
  if (drawerRoot.hidden) return;
  latestPatientRequestId += 1; // ignore a patient reply that arrives after closing
  drawer.classList.add("translate-x-full");
  drawerBackdrop.classList.add("opacity-0");
  pageHeader.inert = false;
  pageMain.inert = false;
  document.body.classList.remove("overflow-hidden");
  drawerRoot.hidden = true;

  // Return focus to the card that opened the drawer, so keyboard users keep their place.
  if (cardThatOpenedDrawer && document.body.contains(cardThatOpenedDrawer)) {
    cardThatOpenedDrawer.focus();
  }
  cardThatOpenedDrawer = null;
}

/** Keep Tab and Shift+Tab inside the drawer while it is open. */
function trapFocus(event) {
  const focusable = [...drawer.querySelectorAll('button, a[href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
    .filter((node) => !node.disabled && node.offsetParent !== null);
  if (focusable.length === 0) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];

  if (event.shiftKey && (document.activeElement === first || document.activeElement === drawerTitle)) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

drawerClose.addEventListener("click", closeDrawer);
drawerBackdrop.addEventListener("click", closeDrawer);
document.addEventListener("keydown", (event) => {
  if (drawerRoot.hidden) return;
  if (event.key === "Escape") {
    event.preventDefault();
    closeDrawer();
  } else if (event.key === "Tab") {
    trapFocus(event);
  }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

showHint();
