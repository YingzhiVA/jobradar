// The jobradar page. Plain JavaScript, no build step: this file is what runs.
// Everything it shows comes from /api/ on the local server, and everything it
// changes goes through a form there or through one of the server's commands.
//
// The page is one of two ways to work, the terminal and an editor being the
// other. So wherever it does something, it also names the file it wrote or
// the command it ran: see `cmd` and `alt` below.
"use strict";

const TOKEN = document.querySelector("meta[name=token]").content;
const $ = (id) => document.getElementById(id);

// ── helpers ────────────────────────────────────────────────────────────────

function h(tag, props, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "html") el.innerHTML = v; // only for HTML the server rendered and escaped
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k in el && k !== "list") el[k] = v;
    else el.setAttribute(k, v);
  }
  el.append(...nodes(kids));
  return el;
}

// Children as nodes. `condition && element` is used throughout to leave a
// piece out, so false and null mean "nothing here", not the text "false".
function nodes(kids) {
  return kids.flat(Infinity)
    .filter((kid) => kid != null && kid !== false)
    .map((kid) => (kid.nodeType ? kid : document.createTextNode(String(kid))));
}

function put(el, ...kids) {
  el.replaceChildren(...nodes(kids));
}

async function api(path, body) {
  const options = { headers: { "X-Jobradar-Token": TOKEN } };
  if (body !== undefined) {
    options.method = "POST";
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, options);
  } catch {
    throw new Error("jobradar is not running any more. Start it again, then reload this page.");
  }
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

let toastTimer;
function toast(message, bad) {
  const el = $("toast");
  el.textContent = message;
  el.className = bad ? "bad" : "";
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), bad ? 7000 : 2600);
}

// Run an async handler from a button: disable it meanwhile, show any error.
function act(fn) {
  return async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      await fn(event);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  };
}

function ask(title, body, okLabel) {
  return new Promise((resolve) => {
    const dialog = $("dialog");
    const close = (answer) => { dialog.close(); resolve(answer); };
    put(dialog, 
      h("h2", {}, title),
      h("div", {}, body),
      h("div", { class: "row end gap" },
        h("button", { onclick: () => close(false) }, "Cancel"),
        h("button", { class: "primary", onclick: () => close(true) }, okLabel || "Continue")),
    );
    dialog.oncancel = () => resolve(false);
    dialog.showModal();
  });
}

const field = (label, hint, control) =>
  h("label", { class: "field" }, h("span", {}, label), hint && h("small", {}, hint), control);
const check = (label, checked, onchange, extra) =>
  h("label", { class: "check" }, h("input", { type: "checkbox", checked, onchange, ...extra }), h("span", {}, label));
const fileUrl = (uid, name) => `/files/${uid}/${encodeURIComponent(name)}?t=${TOKEN}`;
const external = (href, text) => h("a", { href, target: "_blank", rel: "noopener noreferrer" }, text);

// A command or path as it would be typed, with a button that copies it.
const PY = "./.venv/bin/python";
function cmd(text) {
  const copy = h("button", { class: "copy", title: "Copy", onclick: async (event) => {
    event.stopPropagation();
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied.");
    } catch {
      toast("Could not copy; select the text instead.", true);
    }
  } }, "copy");
  return h("span", { class: "cmd" }, h("code", {}, text), copy);
}
const file = (path) => h("code", { class: "path" }, path);

// Hints are everything that says how a thing is done without the page: the
// file and command under each stage, the "Terminal / editor" boxes, the
// command list of an application. They are what lets the two ways of working
// be mixed, and they are also most of the text on a screen, so one switch in
// the header shows or hides them all. It is a class on <body> and the rest is
// CSS (`.how`, `.alt`, `.hint`), so no view needs to know about it. Off until
// asked for; the choice is kept in this browser.
const HINTS_KEY = "jobradar.hints";
function initHints() {
  const box = $("hints");
  const apply = (on) => {
    box.checked = on;
    document.body.classList.toggle("no-hints", !on);
  };
  let stored = null;
  try {
    stored = localStorage.getItem(HINTS_KEY);
  } catch {
    // Storage switched off in this browser: the switch still works, per visit.
  }
  apply(stored === "on");
  box.addEventListener("change", () => {
    apply(box.checked);
    try {
      localStorage.setItem(HINTS_KEY, box.checked ? "on" : "off");
    } catch {
      // As above.
    }
  });
}

// The same thing done without the page: which file, which command.
function alt(...kids) {
  return h("div", { class: "alt" }, h("span", { class: "alt-tag" }, "Terminal / editor"), h("div", {}, kids));
}

// ── app state and shell ────────────────────────────────────────────────────

const app = { state: null, view: "workflow", refresh: null };

async function loadState() {
  app.state = await api("/api/state");
  $("version").textContent = app.state.version ? `v${app.state.version}` : "";
  return app.state;
}

// The workflow is the root of the page: its three phases each have one view
// to work in. The bar shows that, a home entry and then the phases in their
// order. `crumbs` at the top of each view is the way back: Workflow, the
// view, and what is open in it. The phase is left out there, as it is not a
// place of its own to go back to.
const PAGES = [
  ["settings", "Settings", "Set up"],
  ["matches", "Matches", "Search"],
  ["applications", "Applications", "Apply"],
];

function renderNav() {
  put($("nav"),
    h("button", { class: `root ${app.view === "workflow" ? "on" : ""}`, onclick: () => go("workflow") }, "Workflow"),
    h("span", { class: "branch" }),
    PAGES.map(([id, label, phase], i) => [
      i > 0 && h("span", { class: "chev" }, "›"),
      h("button", { class: `leaf ${app.view === id ? "on" : ""}`, onclick: () => go(id) },
        h("small", {}, phase), h("span", {}, label)),
    ]));
}

function crumbs(view, ...deeper) {
  const [, label] = PAGES.find(([id]) => id === view);
  const link = (text, fn) => h("button", { class: "quiet", onclick: fn }, text);
  const trail = [link("Workflow", () => go("workflow"))];
  if (deeper.length) trail.push(link(label, () => { if (view === "applications") openApp = null; go(view); }), ...deeper.map((d) => h("span", {}, d)));
  else trail.push(h("span", {}, label));
  return h("nav", { class: "crumbs" }, trail.flatMap((item, i) => (i ? [h("span", { class: "chev" }, "›"), item] : [item])));
}

function go(view) {
  app.view = view;
  render();
}

// Setup as a newcomer meets it: the five steps in the order the map numbers
// them. While any is open the page is "guiding": Settings opens on the first
// open step, shows each step's state in its tab, and saving one leads to the
// next instead of back to the map.
const SETUP_ORDER = ["key", "cv", "identity", "constraints", "boards"];
const setupSteps = () => (app.state && app.state.setup.steps) || {};
const guiding = () => Boolean(app.state) && !app.state.setup.ready;
const firstOpenStep = () => SETUP_ORDER.find((id) => !setupSteps()[id]) || null;

// Why a search or a draft can't be started yet, for the title of a button
// that is disabled because of it.
const LOCKED = "Finish the setup steps first";
// What a view shows instead of its content while setup is open: what will be
// here, and a way to the step that is next.
function setupFirst(what) {
  const next = firstOpenStep();
  return [
    h("p", { class: "muted" }, what, next ? ` Setup comes first, and the next step is “${SETTING_TITLES[next]}”.` : " Setup comes first."),
    h("div", { class: "row" }, h("button", { onclick: () => { settingsTab = next || "health"; go("settings"); } }, "Continue setup →")),
  ];
}
const practiceRun = () => start("search_practice", {}); // asks first: see PAID
const PRACTICE_TITLE = "Scores made-up sample postings against your real profile: a few cents, about a minute";

// The address keeps the place, so a reload or the back button returns to it:
// #workflow, #matches, #applications/055, #settings/boards.
function remember() {
  let place = app.view;
  if (app.view === "applications" && openApp) place += `/${openApp}`;
  else if (app.view === "settings") place += `/${settingsTab}`;
  if (location.hash.slice(1) !== place) history.replaceState(null, "", `#${place}`);
}

function recall() {
  const [view, detail] = location.hash.slice(1).split("/");
  if (!["workflow", "matches", "applications", "settings"].includes(view)) return;
  app.view = view;
  if (view === "applications") openApp = detail || null;
  if (view === "settings" && SETTINGS.some(([id]) => id === detail)) settingsTab = detail;
}

function render() {
  // Settings not opened yet: start where there is something to do.
  if (app.view === "settings" && !settingsTab) settingsTab = firstOpenStep() || "search";
  remember();
  renderNav();
  const main = $("main");
  app.refresh = null;
  ({ workflow: renderWorkflow, matches: renderMatches, applications: renderApplications, settings: renderSettings })[app.view](main);
}

// ── running commands ───────────────────────────────────────────────────────

// `note` replaces "Done." for a command that ended cleanly without doing what
// was asked of it (a draft whose posting could not be read), or says what it
// found (the pre-submission check). `verdict`, [text, pill class], replaces
// the finished pill where "Stopped with a problem" would be wrong: a check
// that finds problems has worked.
const job = { open: false, lines: [], data: null, timer: null, onDone: null, note: null, verdict: null };

// The actions that call the model, and so are billed to the user's API key.
// `start` asks before any of them, whichever button it came from, so a new
// button for a paid action cannot forget to. Each entry is the dialogue's
// title, what the money buys, and the confirm label; the figures are those of
// docs/COSTS.md. The search has a longer dialogue of its own (confirmSearch).
const PAID = {
  search_practice: () => ["Run a practice search?",
    "Claude scores a handful of made-up postings against your real profile, to show that everything works. A few cents.",
    "Run and pay"],
  draft: (p) => ["Draft this application?",
    `Claude reads the posting and writes a tailored CV${p.full ? " and a cover letter" : ", plus a cover letter if the posting asks for one"}. About 25 to 45 cents, a little more for a posting that is not in English.`,
    "Draft and pay"],
  upgrade: () => ["Re-draft with a cover letter?",
    "Claude writes the tailored CV again together with a cover letter, which replaces the current tailored CV. About the cost of a new draft: 25 to 45 cents.",
    "Re-draft and pay"],
  prep: () => ["Prepare the interview sheet?",
    "Claude maps the questions this posting is likely to bring onto your stories and write-ups. About 15 to 20 cents.",
    "Prepare and pay"],
};

async function confirmPaid(action, params) {
  if (action === "search") return confirmSearch();
  if (!PAID[action]) return true;
  const [title, what, label] = PAID[action](params);
  return ask(title, h("p", {}, h("strong", {}, "This costs money. "), what, " Anthropic bills it to your API key."), label);
}

async function start(action, params, onDone) {
  if (!(await confirmPaid(action, params))) return null;
  const result = await api("/api/run", { action, ...params });
  if (!result.job) return result; // a quick command: finished already
  job.lines = [];
  job.note = null;
  job.verdict = null;
  job.data = result.job;
  job.onDone = onDone || null;
  pollJob();
  // The view was drawn while nothing ran: draw it again, so that what may
  // not be pressed while a command runs is switched off or left out.
  if (app.refresh) app.refresh();
  return result;
}

async function pollJob() {
  clearTimeout(job.timer);
  let data;
  try {
    data = (await api(`/api/job?since=${job.lines.length}`)).job;
  } catch (error) {
    toast(error.message, true);
    return;
  }
  if (!data) return;
  job.lines.push(...data.lines);
  job.data = data;
  renderJob();
  if (data.state === "running") {
    job.timer = setTimeout(pollJob, 1000);
  } else {
    const done = job.onDone;
    job.onDone = null;
    await loadState().catch(() => {});
    if (done) await done(data);
    else if (app.refresh) app.refresh();
  }
}

function renderJob() {
  const el = $("job");
  const data = job.data;
  if (!data) return (el.hidden = true);
  const running = data.state === "running";
  const minutes = Math.floor(data.seconds / 60);
  const elapsed = minutes ? `${minutes} min ${data.seconds % 60} s` : `${data.seconds} s`;
  const verdict = { done: "Finished", failed: "Stopped with a problem", cancelled: "Cancelled" }[data.state];
  const last = job.lines[job.lines.length - 1] || "Starting…";
  el.hidden = false;
  put(el, 
    h("div", { class: "head" },
      running ? h("span", { class: "spin" })
        : job.verdict ? h("span", { class: `pill ${job.verdict[1]}` }, job.verdict[0])
        : h("span", { class: `pill ${data.state === "done" ? "good" : "bad"}` }, verdict),
      h("strong", { class: "grow" }, data.label),
      h("span", { class: "muted small" }, elapsed),
      h("button", { class: "quiet", onclick: () => { job.open = !job.open; renderJob(); } }, job.open ? "Hide details" : "Details"),
      running
        ? h("button", { class: "danger", onclick: act(async () => { await api("/api/cancel", {}); }) }, "Stop")
        : h("button", { onclick: () => { job.data = null; renderJob(); } }, "Close")),
    h("div", { class: "line" }, running ? last : job.note || (data.state === "done" ? "Done." : last)),
    job.open && data.command && h("div", { class: "ran" }, "Ran: ", cmd(data.command)),
    job.open && h("pre", { class: "out" }, job.lines.join("\n")),
  );
  if (job.open) el.querySelector("pre").scrollTop = 1e9;
}

const busy = () => job.data && job.data.state === "running";
// Whether the running command works on this application: then its status
// can't be changed until the command ends or is stopped.
const busyWith = (a) => busy() && (
  (job.data.uid && Number(job.data.uid) === Number(a.uid)) || (job.data.url && job.data.url === a.url));

// ── setup steps (shown under Settings, reached from the workflow) ──────────
// Each takes the element to fill and a function to call once it is saved.

const CONSOLE = "https://platform.claude.com/";

async function stepKey(box, done) {
  const status = (await loadState()).key;
  // A text field masked by CSS, not a password field: browsers offer to save
  // what is typed into a password field, and a saved password may be synced
  // to the browser maker's cloud. The data-* attributes ask the common
  // password-manager extensions to leave the field alone too.
  const input = h("input", {
    type: "text", class: "secret", placeholder: "sk-ant-…", autocomplete: "off", spellcheck: false,
    autocapitalize: "off", "data-1p-ignore": "true", "data-lpignore": "true", "data-bwignore": "true", "data-form-type": "other",
  });
  const result = h("div", {});
  const save = act(async () => {
    const answer = await api("/api/key", { key: input.value });
    if (!answer.works) {
      put(result, h("div", { class: "note bad" }, answer.error));
      return;
    }
    input.value = "";
    toast("The key works.");
    done();
  });
  put(box, 
    h("h1", {}, "Connect Claude"),
    h("p", {}, "jobradar uses Claude to read postings and write drafts. For that it needs an API key from your own Claude account; you pay Anthropic directly for what it uses."),
    h("div", { class: "card" },
      h("ol", { class: "steps-list" },
        h("li", {}, "Open the ", external(CONSOLE, "Claude Console"), " and sign in, or create an account."),
        h("li", {}, "Under ", h("strong", {}, "Billing"), ", add credit. A new account has none; $10 covers your first search and more than a week of daily ones."),
        h("li", {}, "Under ", h("strong", {}, "API keys"), ", create a key and name it ", h("strong", {}, "jobradar"), ". Give it a monthly spend limit: the tool can then never spend more than you chose."),
        h("li", {}, "Copy the key when it is shown. It starts with ", h("code", {}, "sk-ant-"), " and the Console shows it only once. Paste it here.")),
      status.set && h("div", { class: "note" }, `A key is already stored (${status.hint}). Paste a new one only if you want to replace it.`),
      field("Your key", "Stored in a file on this computer and sent only to Claude.", input),
      result,
      h("div", { class: "row end" },
        status.set && h("button", { onclick: done }, "Keep the stored key"),
        h("button", { class: "primary", onclick: save }, "Save and test"))),
  );
}

function readAsBase64(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.readAsDataURL(file);
  });
}

async function stepCv(box, done) {
  const { cvs } = await api("/api/cvs");
  const real = cvs.filter((cv) => !cv.template);
  const again = () => stepCv(box, done);

  const upload = async (file) => {
    try {
      const saved = await api("/api/cvs", { filename: file.name, data: await readAsBase64(file) });
      toast(`Added ${saved.name}. Read it through once.`);
      await again();
      editCv(saved.name, again);
    } catch (error) {
      toast(error.message, true);
    }
  };
  const picker = h("input", { type: "file", accept: ".docx,.md,.txt", hidden: true, onchange: (e) => e.target.files[0] && upload(e.target.files[0]) });
  const drop = h("div", {
    class: "drop", tabIndex: 0, role: "button",
    onclick: () => picker.click(),
    onkeydown: (e) => (e.key === "Enter" || e.key === " ") && picker.click(),
    ondragover: (e) => { e.preventDefault(); drop.classList.add("over"); },
    ondragleave: () => drop.classList.remove("over"),
    ondrop: (e) => { e.preventDefault(); drop.classList.remove("over"); if (e.dataTransfer.files[0]) upload(e.dataTransfer.files[0]); },
  }, h("strong", {}, "Drop your CV here, or click to choose it"), h("div", { class: "small" }, "Word (.docx), or a text file (.md, .txt)"));

  const name = h("input", { type: "text", placeholder: "e.g. product-manager" });
  const text = h("textarea", { class: "tall", placeholder: "Paste the full text of your CV here." });
  const paste = act(async () => {
    await api("/api/cvs", { name: name.value || "my-cv", text: text.value });
    toast("CV saved.");
    again();
  });

  put(box, 
    h("h1", {}, "Your CV"),
    h("p", {}, "This is what postings are compared against, and what your tailored CVs are made from."),
    h("div", { class: "note" }, h("strong", {}, "Use your full CV, not one you trimmed for an earlier application. "), "A tailored CV is made by cutting from this one, and nothing is ever added that it doesn’t contain. Experience left out here can’t earn you a match and can’t appear in an application."),
    h("div", { class: "card" },
      real.length > 0 && h("ul", { class: "cv-list" }, real.map((cv) =>
        h("li", {},
          h("strong", { class: "grow" }, cv.name),
          h("span", { class: "muted small" }, `${cv.words} words${cv.from_word ? `, from ${cv.from_word}` : ""}`),
          h("button", { onclick: () => editCv(cv.name, again) }, "Read and edit"),
          h("button", { class: "danger", onclick: act(async () => {
            if (!(await ask("Remove this CV?", h("p", {}, `${cv.name} will be deleted from this computer.`), "Remove"))) return;
            await api("/api/cvs", { delete: cv.name });
            again();
          }) }, "Remove")))),
      drop, picker,
      h("details", { class: "gap" },
        h("summary", {}, "Or paste the text instead"),
        field("A short name for this CV", "It appears in reports as the CV that fits a posting best.", name),
        field("CV text", null, text),
        h("div", { class: "row end" }, h("button", { onclick: paste }, "Save pasted CV"))),
      real.length > 0 && h("p", { class: "muted small gap" }, "Applying to more than one kind of role? Add one CV per kind. Each should still cover all your experience and differ in emphasis only.")),
    h("div", { class: "row end" },
      real.length > 0 && h("button", { onclick: done }, guiding() ? "Continue →" : "Done")),
  );
}

async function editCv(name, after) {
  const { text } = await api(`/api/cvs?name=${encodeURIComponent(name)}`);
  const area = h("textarea", { class: "tall", value: text });
  const dialog = $("dialog");
  put(dialog, 
    h("h2", {}, name),
    h("p", { class: "muted small" }, "This is the text jobradar will read. A two-column Word layout comes out one column after the other; check that nothing is missing or out of order, and fix it here if so."),
    area,
    h("div", { class: "row end gap" },
      h("button", { onclick: () => dialog.close() }, "Close"),
      h("button", { class: "primary", onclick: act(async () => {
        await api("/api/cvs", { name, text: area.value });
        dialog.close();
        toast("CV saved.");
        after();
      }) }, "Save changes")),
  );
  dialog.oncancel = null;
  dialog.showModal();
}

// A setup form's save button, which also moves on while setup is still open.
const saveLabel = () => (guiding() ? "Save and continue" : "Save");

async function stepIdentity(box, done) {
  const data = await api("/api/identity");
  const areas = data.sections.map((s) => ({ ...s, el: h("textarea", { value: s.body }) }));
  const save = act(async () => {
    await api("/api/identity", { sections: areas.map((s) => ({ heading: s.heading, body: s.el.value })) });
    toast("Saved.");
    done();
  });
  put(box, 
    h("h1", {}, "What you want next"),
    h("p", {}, "Your CV says what you can do. This page says what you want to do, and it is what stops the tool from showing you jobs you are qualified for but would never take."),
    h("div", { class: "note" }, "Write it as you would explain yourself to a thoughtful mentor, in plain sentences, in any language. Vague text gets vague scores: “something with impact” helps nobody, “a product role at a company under 200 people, close to the data” does."),
    h("div", { class: "card" }, areas.map((s) => field(s.label, s.hint, s.el))),
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: save }, saveLabel())),
  );
}

async function stepConstraints(box, done) {
  const c = await api("/api/constraints");
  const chosen = new Set(c.allowed_regions);
  const boxes = h("div", { class: "regions" }, c.regions.map((region) =>
    check(region, chosen.has(region), (e) => (e.target.checked ? chosen.add(region) : chosen.delete(region)))));
  const setAll = (on) => {
    c.regions.forEach((r) => (on ? chosen.add(r) : chosen.delete(r)));
    boxes.querySelectorAll("input").forEach((i) => (i.checked = on));
  };
  const remote = h("input", { type: "checkbox", checked: c.remote_in_country });
  const low = h("input", { type: "number", min: 10, max: 100, step: 10, value: c.min_percentage });
  const high = h("input", { type: "number", min: 10, max: 100, step: 10, value: c.max_percentage });
  const days = h("select", {},
    h("option", { value: "" }, "No limit"),
    [0, 1, 2, 3, 4, 5].map((n) => h("option", { value: n, selected: c.max_office_days === n }, n === 0 ? "0 (fully remote)" : `${n}`)));
  const breakers = h("textarea", { value: c.dealbreakers.join("\n"), placeholder: "e.g. Internship or student role" });
  const save = act(async () => {
    await api("/api/constraints", {
      allowed_regions: [...chosen], remote_in_country: remote.checked,
      min_percentage: low.value, max_percentage: high.value,
      max_office_days: days.value, dealbreakers: breakers.value,
    });
    toast("Saved.");
    done();
  });
  put(box, 
    h("h1", {}, "Your must-haves"),
    h("p", {}, "A posting that fails one of these is thrown out before anything is read or paid for, however good it looks otherwise. Keep them to what is really non-negotiable; preferences belong under “What you want”."),
    h("div", { class: "card" },
      h("div", { class: "row between" },
        h("span", { class: "label" }, `Where the job may be (${c.regions_word})`),
        h("span", {}, h("button", { class: "quiet", onclick: () => setAll(true) }, "All"), h("button", { class: "quiet", onclick: () => setAll(false) }, "None"))),
      h("span", { class: "hint" }, "Tick every one you could commute to. A posting in a small town is matched to its canton for you."),
      boxes,
      h("div", { class: "gap" }, h("label", { class: "check" }, remote, h("span", {}, `Also show fully remote jobs within ${c.country}`)))),
    h("div", { class: "card" },
      h("span", { class: "label" }, "Workload"),
      h("span", { class: "hint" }, "A posting passes if its range overlaps yours. 80 to 100 means part-time at 80% or full-time."),
      h("div", { class: "row" }, "From", low, "% to", high, "%"),
      h("div", { class: "gap" }),
      field("Most days a week in the office", "Postings that say they require more are dropped. Most don’t say, and those are kept.", days)),
    h("div", { class: "card" },
      field("Other deal-breakers", "One per line, in your own words. These are judged when a posting is read.", breakers)),
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: save }, saveLabel())),
  );
}

function boardCost(count, data) {
  if (count === 0) return ["", "No company selected: only the web search runs, about 5 to 10 cents a day."];
  if (!data.first_run) return ["", `${count} selected. Each newly added company costs one catch-up read of its open postings, the first time only.`];
  if (count <= 10) return ["", `${count} selected. Your first search reads every open posting at these companies: usually $1 to $2. After that, a day costs cents.`];
  if (count <= data.many) return ["warn", `${count} selected. Your first search reads every open posting at all of them and may cost a few dollars. Starting with five to ten is cheaper.`];
  return ["warn", `${count} selected. Your first search reads every open posting at all of them: roughly $4 to $10 with everything ticked, against cents on a normal day. Consider starting with five to ten and adding more later.`];
}

async function stepBoards(box, done) {
  const data = await api("/api/boards");
  const chosen = new Set(data.sections.flatMap((s) => s.boards.filter((b) => b.selected).map((b) => b.name)));
  const cost = h("div", {});
  const updateCost = () => {
    const [kind, message] = boardCost(chosen.size + data.own.length, data);
    cost.className = `grow small ${kind === "warn" ? "note warn" : "muted"}`;
    cost.textContent = message;
  };
  const sections = data.sections.map((section) => {
    const inputs = [];
    const count = h("span", { class: "muted small" });
    const recount = () => { count.textContent = `${section.boards.filter((b) => chosen.has(b.name)).length} of ${section.boards.length}`; };
    const items = section.boards.map((board) => {
      const label = check(board.name, chosen.has(board.name), (e) => {
        e.target.checked ? chosen.add(board.name) : chosen.delete(board.name);
        recount(); updateCost();
      });
      label.dataset.name = board.name.toLowerCase();
      inputs.push([board.name, label.querySelector("input")]);
      return label;
    });
    const setAll = (on) => {
      inputs.forEach(([name, input]) => { input.checked = on; on ? chosen.add(name) : chosen.delete(name); });
      recount(); updateCost();
    };
    recount();
    return h("section", {},
      h("h3", {}, section.name, count,
        h("button", { class: "quiet small", onclick: () => setAll(true) }, "all"),
        h("button", { class: "quiet small", onclick: () => setAll(false) }, "none")),
      h("div", { class: "grid" }, items));
  });
  const list = h("div", { class: "boards" }, sections);
  const search = h("input", { type: "search", placeholder: "Find a company…", oninput: (e) => {
    const q = e.target.value.trim().toLowerCase();
    list.querySelectorAll("label.check").forEach((l) => (l.hidden = q !== "" && !l.dataset.name.includes(q)));
    list.querySelectorAll("section").forEach((s) => (s.hidden = !s.querySelector("label.check:not([hidden])")));
  } });
  const save = act(async () => {
    await api("/api/boards", { names: [...chosen] });
    toast("Saved.");
    done();
  });
  updateCost();
  put(box, 
    h("h1", {}, "Where to search"),
    h("p", {}, "jobradar reads the careers pages of the companies you tick, every time it searches. It also runs a web search based on your profile, which finds postings elsewhere, so this list doesn’t have to be complete."),
    h("div", { class: "note" }, h("strong", {}, "Start with five to ten companies you would really like to work for. "), "The first search reads everything currently open at each one, so a long list makes the first day expensive. You can add more at any time."),
    data.problems.length > 0 && h("div", { class: "note warn" }, data.problems.join(" · ")),
    h("div", { class: "card" },
      search,
      data.own.length > 0 && h("p", { class: "muted small gap" }, `Also scanned, added by hand in companies.yaml: ${data.own.join(", ")}.`),
      list),
    h("div", { class: "sticky-bar row" }, cost, h("button", { class: "primary", onclick: save }, saveLabel())),
  );
}

const CHECK_NAMES = {
  "Anthropic credentials": "Claude key", "CVs": "Your CV", "Identity": "What you want",
  "companies.yaml": "Companies", "constraints.yaml": "Must-haves", "search.yaml": "Search settings",
  "retention.yaml": "Clean-up settings", "boards": "Company list", "PDF export": "PDF export",
};
// What a passed check says. The doctor's own wording names files; here the
// step's name already says what was checked.
const OK_TEXT = {
  "Anthropic credentials": "Connected", "Identity": "Written", "constraints.yaml": "Set",
  "PDF export": "Ready", "search.yaml": "Set",
};
const TECHNICAL = new Set(["Python", "Git merge driver", "retention.yaml", "boards"]);

// The doctor's messages say "boards" where the page says "companies". A path
// (config/boards/) keeps its name, and "company boards" becomes one word, not
// "company companies".
const pageWords = (message) => message
  .replace(/\bcompany boards\b/g, "companies")
  .replace(/(^|[\s(])boards\b(?!\/)/g, "$1companies");

async function checksList(fresh) {
  const result = await api(`/api/doctor${fresh ? "?fresh=1" : ""}`);
  lastCheck = result;
  const { checks, failed } = result;
  const shown = checks.filter((c) => c.level !== "ok" || !TECHNICAL.has(c.name)).filter((c) => c.name !== "Git merge driver");
  const icon = { ok: "✓", warn: "!", FAIL: "✗" };
  return {
    failed,
    list: h("ul", { class: "checks" }, shown.map((c) =>
      h("li", {}, h("strong", { class: c.level }, icon[c.level]), h("strong", {}, CHECK_NAMES[c.name] || c.name),
        h("span", {}, c.level === "ok" && OK_TEXT[c.name] ? OK_TEXT[c.name] : pageWords(c.message))))),
  };
}

function searchCost(boards) {
  if (boards.first_run) return boardCost(boards.selected, boards)[1];
  if (boards.selected === 0) return "With no company selected only the web search runs: about 5 to 10 cents.";
  return `With ${boards.selected} ${boards.selected === 1 ? "company" : "companies"} selected, a normal day is about 10 to 20 cents. Companies added since the last run cost one catch-up read of everything open there.`;
}

const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};

// The search's own confirmation (see PAID for the other paid actions): it
// has an estimate that depends on the boards selected, and the cloud reminder.
async function confirmSearch() {
  const [boards, overview] = await Promise.all([api("/api/boards"), api("/api/overview")]);
  const run = overview.last_run;
  const ranToday = run && !run.practice && run.date === today();
  return ask("Run a search now?",
    [h("p", {}, h("strong", {}, "This costs money. "), "Claude reads and scores every posting that is new, and Anthropic bills your API key for it. ", searchCost(boards)),
     h("p", { class: "muted" }, "It usually takes a few minutes; the first time can take longer."),
     ranToday && h("p", { class: "note warn" }, `A search already ran today (${run.date}). Another one only finds what was posted since.`),
     h("p", { class: "note" }, h("strong", {}, "Is the cloud schedule on for this copy? "),
       "It already searches every morning, and it works from what is on GitHub, not from this computer. Pull before you search here and commit and push afterwards, or the same postings are read and paid for twice.")],
    "Search and pay");
}

// ── archives ───────────────────────────────────────────────────────────────
// Archived reports and applications are folded away under their live ones.
// Only their number is known up front; the first page is read when the
// section is unfolded, and each further page on request. What was read stays
// in `store` while the page is open, so folding and unfolding costs nothing.

function archiveStore(path) {
  return { path, open: false, items: [], next: 0, total: null, loading: false };
}

async function loadArchivePage(store) {
  if (store.loading || store.next === null) return;
  store.loading = true;
  try {
    const page = await api(`${store.path}?offset=${store.next}`);
    store.items.push(...(page.reports || page.applications));
    store.total = page.total;
    store.next = page.next;
  } finally {
    store.loading = false;
  }
}

// `count` is what the live view already knows; `body` draws the items read
// so far; `rerender` draws the whole view again after a toggle or a load.
function archiveSection(store, { title, count, unit, body, rerender }) {
  if (!count) return null;
  const toggle = act(async () => {
    store.open = !store.open;
    if (store.open && store.items.length === 0) await loadArchivePage(store);
    rerender();
  });
  const more = act(async () => { await loadArchivePage(store); rerender(); });
  const left = (store.total ?? count) - store.items.length;
  return h("section", { class: "archive" },
    h("button", { class: "archive-head", "aria-expanded": String(store.open), onclick: toggle },
      h("span", { class: "caret" }, store.open ? "▾" : "▸"),
      h("h2", {}, title),
      h("span", { class: "muted" }, `${count}`)),
    store.open && [
      body(store.items),
      store.next !== null && h("button", { class: "quiet", onclick: more }, `Show ${Math.min(left, ARCHIVE_STEP[unit])} more of ${left}`),
    ]);
}

// Page sizes, as the server reads them (views.REPORT_ARCHIVE_PAGE, ARCHIVE_PAGE).
const ARCHIVE_STEP = { reports: 20, applications: 50 };
const reportArchive = archiveStore("/api/report/archive");
const applicationArchive = archiveStore("/api/applications/archive");

// ── workflow ───────────────────────────────────────────────────────────────
// The whole of jobradar on one page: what happens in which order, where each
// stage stands in this copy, and how to do it here or in a terminal.

function workflow(o, check) {
  const step = o.setup.steps;
  const run = o.last_run;
  const n = (count, one, many) => `${count} ${count === 1 ? one : many}`;
  // Only the first open setup step is "next"; the ones after it are merely
  // not done yet, and five highlighted boxes would point nowhere.
  const firstOpen = ["key", "cv", "identity", "constraints", "boards"].find((id) => !step[id]);
  const setupStage = (id, title, what, where, detail) => ({
    id, title, what, detail, files: [where], open: ["settings", id],
    state: step[id] ? "done" : id === firstOpen ? "act" : "info",
    pill: step[id] ? "Done" : id === firstOpen ? "Next" : "To do",
    button: step[id] ? "Change" : "Fill in",
  });
  const failed = check ? check.checks.filter((c) => c.level === "FAIL") : [];
  const warned = check ? check.checks.filter((c) => c.level === "warn" && c.name !== "Git merge driver") : [];
  const names = (list) => list.map((c) => CHECK_NAMES[c.name] || c.name).join(", ");
  const waiting = (count, text) => (count ? { state: "act", pill: text } : { state: "info" });
  return [
    { name: "Set up", note: "Once. Every step is a file in your copy: fill it in here or in your editor. ", stages: [
      setupStage("key", "Connect Claude", "An API key from your own Claude account. Claude does the reading and writing; you pay Anthropic for it directly.", ".env"),
      setupStage("cv", "Your CV", "What postings are scored against, and what tailored CVs are cut from. Markdown or Word, one per kind of role.", "profile/cvs/"),
      setupStage("identity", "What you want", "A page in your own words on where you are heading and what you are leaving. It is what the interest score means.", "profile/identity.md"),
      setupStage("constraints", "Must-haves", "Cantons, workload, office days. A posting that fails one is dropped before anything is read or paid for.", "config/constraints.yaml"),
      setupStage("boards", "Where to search", "The company career pages to scan, picked by name from the shipped catalogue. A web search runs besides.", "config/companies.yaml",
        `${n(o.boards, "company", "companies")} selected`),
      { id: "health", title: "Check", what: "Tests the key and reads through the profile and config, so a mistake is named before a run is spent on it. This page runs it on every visit.",
        command: `${PY} -m jobradar.doctor`, open: ["settings", "health"], button: "See the check",
        // Until the steps above are filled in, it can only repeat that they
        // aren't: it becomes a stage of its own once they are.
        state: firstOpen || !check ? "info" : failed.length ? "act" : "done",
        pill: firstOpen ? "After the steps above" : !check ? "Checking…" : failed.length ? n(failed.length, "problem", "problems") : warned.length ? `Passes, ${n(warned.length, "warning", "warnings")}` : "Passes",
        detail: firstOpen ? "" : failed.length ? `To fix: ${names(failed)}.` : warned.length ? `Worth a look: ${names(warned)}.` : "" },
    ] },
    { name: "Search", note: "Press the button, type the command, or let the cloud schedule do it and email you (docs/CLOUD.md).", stages: [
      { title: "Search", what: "Fetches the postings on your boards and from the web, keeps the new ones, filters them by your must-haves, and scores what is left on skill fit and interest fit.",
        detail: !run ? "" : `${(run.found ?? 0).toLocaleString()} found → ${run.new ?? 0} new → ${run.passed ?? 0} passed must-haves → ${run.scored ?? 0} scored → ${run.surfaced ?? 0} surfaced.`,
        command: `${PY} -m jobradar.search.main`,
        ...(o.setup.ready && (!run || run.practice) ? { state: "act", pill: "Next" } : { state: "info", pill: run ? `Last run ${run.date}${run.practice ? " (practice)" : ""}` : o.setup.ready ? "Not run yet" : "After setup" }),
        action: "search", button: "Search now",
        extra: { label: "Practice run", action: "practice", title: PRACTICE_TITLE, command: `${PY} -m jobradar.search.main --dry-run` } },
      { title: "Review the matches", what: "A handful per run, each with both scores, why it fits, and how to tailor your application. A run with no matches is normal.",
        detail: o.report_date ? `Newest report: ${o.report_date}. ${n(o.reports, "report", "reports")} not yet archived.` : "",
        // Never "waiting for you": nothing records that a match was read, so
        // a marker here could only be cleared by the next report.
        files: ["reports/<date>.md", "reports/runs.jsonl"], state: "info", pill: o.report_date ? `${n(o.matches, "match", "matches")} in the newest report` : "",
        open: ["matches"], button: "Open matches" },
    ] },
    { name: "Apply", note: "For each posting you decide to go for, from a report or found yourself.", stages: [
      { title: "Draft", what: "Fetches the posting and writes a tailored CV, a cover letter when one is asked for, and notes on fit, gaps and what was cut. In the posting’s language.",
        detail: o.needs_jd ? "A posting that could not be read waits for you to paste the job text." : "",
        command: `${PY} -m jobradar.apply --url <address>`, ...waiting(o.needs_jd, `${o.needs_jd} need the job text`), open: [o.needs_jd ? "applications" : "matches"], button: o.needs_jd ? "Open applications" : "Draft from a match",
        more: { label: "Draft a posting you found", title: "Paste the address of a posting the search didn’t find", fn: () => { wantPaste = true; openApp = null; go("applications"); } } },
      { title: "Edit and check", what: "Read what was written, change what you would say differently, run the pre-submission check, make the PDFs.",
        files: ["applications/<id>_…/"], command: `${PY} -m jobradar.apply --lint <id>`, ...waiting(o.drafted, `${n(o.drafted, "draft", "drafts")} not sent`), open: ["applications"], button: "Open applications" },
      { title: "Send and record", what: "You submit on the employer’s site yourself; jobradar never sends anything. Then record that you did, which starts the clock for follow-up and archiving.",
        command: `${PY} -m jobradar.apply --mark-submitted <id>`, state: "info", open: ["applications"], button: "Open applications" },
      { title: "Outcome", what: "Record what came back. For an interview, a prep sheet maps the likely questions onto your stories.",
        command: `${PY} -m jobradar.apply --mark-rejected <id>   (or --mark-offer, --mark-withdrawn, --prep)`,
        state: "info", pill: o.submitted ? `${o.submitted} awaiting an answer` : "", open: ["applications"], button: "Open applications" },
    ] },
  ];
}

const SIDE_TASKS = (o) => [
  ["Steer how much you see", "Score floors and matches per run", null, ["settings", "search"]],
  ["Find more companies to scan", "docs/DISCOVERY.md", `${PY} -m jobradar.discovery.discover`],
  ["Check that your boards still answer", "docs/DISCOVERY.md", `${PY} -m jobradar.board_health`],
  ["Add interview stories", "profile/stories/README.md", null],
  ["Ingest your published write-ups", "docs/SETUP.md", `${PY} -m jobradar.evidence --site <url>`],
  ["Archive old reports and applications", "docs/APPLY.md", `${PY} -m jobradar.archive`],
  o.rav && [o.rav_unfiled.length ? `File a RAV month (not filed: ${o.rav_unfiled.join(", ")})` : "File a RAV month", "docs/APPLY.md",
    `${PY} -m jobradar.apply --rav-filed <YYYY-MM>`, ["applications", "rav"]],
  ["Run it every morning without your computer", "docs/CLOUD.md", null],
  ["Get updates", "docs/SETUP.md", "git pull upstream master"],
].filter(Boolean);

function nextUp(o, phases, check) {
  const open = phases[0].stages.find((stage) => stage.state === "act");
  if (open) return [open.id === "health" ? "The setup check found a problem." : `Next: ${open.title}.`, open.button, () => openStage(open)];
  // A fourth entry is a second, lesser button. The practice run is offered
  // before the first search only: it is the cheap way to see the whole loop.
  if (!o.last_run) return ["Setup is complete. Next: your first search.", "Search now", runSearch,
    ["Practice run", practiceRun, PRACTICE_TITLE],
    " To see how it works first, a practice run scores made-up postings against your real profile for a few cents."];
  if (o.last_run.practice) return ["The practice run worked. Next: your first real search.", "Search now", runSearch];
  if (o.needs_jd) return [`${o.needs_jd} posting${o.needs_jd > 1 ? "s" : ""} could not be read and need the job text pasted in.`, "Open applications", () => go("applications")];
  if (o.drafted) return [`${o.drafted} draft${o.drafted > 1 ? "s" : ""} to finish and send.`, "Open applications", () => go("applications")];
  // Nothing waiting: no banner. Search keeps its own button in stage 7.
  return null;
}

function openStage(stage) {
  if (stage.open[0] === "settings") settingsTab = stage.open[1];
  go(stage.open[0]);
}

async function runSearch() {
  await start("search", {}); // asks first: see PAID
}

// The last setup-check result, kept between visits so the page can draw at
// once and correct itself when the fresh answer arrives.
let lastCheck = null;
// Whether the setup steps are folded away. null: not chosen by hand, so they
// are open exactly while something in them still needs doing.
let setupOpen = null;

// One marker means one thing on every stage:
//   done  setup only: this step is filled in / the check passes
//   act   something here is waiting for you
//   info  nothing to do; the pill only reports
const PILL = { done: "good", act: "act", info: "grey" };

async function renderWorkflow(main, fresh) {
  app.refresh = () => renderWorkflow(main);
  let o;
  try {
    o = await api("/api/overview");
  } catch (error) {
    return put(main, h("div", { class: "note bad" }, error.message));
  }
  const phases = workflow(o, lastCheck);
  const next = nextUp(o, phases, lastCheck);
  const setup = phases[0].stages;
  const setupDone = setup.every((stage) => stage.state === "done" || stage.id === "health" && stage.state !== "act");
  const open = setupOpen ?? !setupDone;
  let number = 0;
  const stageRow = (stage) => {
    number += 1;
    const buttons = [];
    // Filled means "this acts" (see the button rule in style.css). On the map
    // only Search does; every other stage button leads to a view, and the
    // orange marker is what says a stage is waiting.
    const locked = !o.setup.ready && LOCKED;
    if (stage.action === "search") buttons.push(h("button", { class: "primary", title: locked, disabled: busy() || !o.setup.ready, onclick: act(runSearch) }, stage.button));
    else if (stage.open) buttons.push(h("button", { onclick: () => openStage(stage) }, stage.button));
    if (stage.more) buttons.push(h("button", { title: stage.more.title, onclick: stage.more.fn }, stage.more.label));
    if (stage.extra) buttons.push(h("button", { title: locked || stage.extra.title, disabled: busy() || !o.setup.ready, onclick: act(practiceRun) }, stage.extra.label));
    return h("li", { class: `stage ${stage.state}` },
      h("span", { class: "num" }, number),
      h("div", { class: "body" },
        h("div", { class: "row between" },
          h("div", { class: "row" }, h("h3", {}, stage.title), stage.pill && h("span", { class: `pill ${PILL[stage.state]}` }, stage.pill)),
          h("div", { class: "row" }, buttons)),
        h("p", {}, stage.what),
        stage.detail && h("p", { class: "detail" }, stage.detail),
        h("div", { class: "how" },
          (stage.files || []).map(file),
          stage.command && cmd(stage.command),
          stage.extra && cmd(stage.extra.command))));
  };
  const phaseHead = (phase, page) => h("div", { class: "row between" },
    h("h2", {}, phase.name),
    page && h("button", { class: "quiet", onclick: () => go(page[0]) }, `${page[1]} →`));
  const setupSummary = () => {
    number += setup.length;
    const toggle = h("button", { class: "quiet", onclick: () => { setupOpen = true; renderWorkflow(main); } }, "Show the steps");
    return h("div", { class: "stage done folded" },
      h("span", { class: "num" }, `1–${setup.length}`),
      h("div", { class: "body row between" },
        h("div", {},
          h("div", { class: "row" }, h("h3", {}, "All set up"), h("span", { class: "pill good" }, lastCheck ? "Done, check passes" : "Done")),
          h("p", { class: "muted" }, setup.map((stage) => stage.title).join(" · "), ` · ${o.boards} ${o.boards === 1 ? "company" : "companies"} selected`)),
        toggle));
  };
  put(main,
    h("h1", {}, "Jobradar workflow"),
    h("p", { class: "muted" }, "Three phases: one-time setup, search relevant postings, then apply on demand. Every stage can be done on this page or in a terminal and your editor, and the two ways can be mixed freely.",
      h("span", { class: "hint" }, " The grey line under each stage names the file it lives in or the command that runs it."),
      h("span", { class: "no-hint" }, " Tick “Show commands”, top right, to see the file or command behind each stage.")),
    next && h("div", { class: "note next row between" },
      h("span", { class: "grow" }, next[0], next[4] && h("span", { class: "plain" }, next[4])),
      next[3] && h("button", { title: next[3][2], disabled: busy(), onclick: act(next[3][1]) }, next[3][0]),
      h("button", { class: next[2] === runSearch ? "primary" : "", disabled: busy(), onclick: act(next[2]) }, next[1])),
    phases.map((phase, i) => h("section", { class: "phase" },
      phaseHead(phase, PAGES[i]),
      h("p", { class: "muted small" }, phase.note,
        i === 0 && open && setupDone && h("button", { class: "quiet", onclick: () => { setupOpen = false; renderWorkflow(main); } }, "Hide the steps")),
      i === 0 && !open
        ? h("ol", { class: "flow" }, setupSummary())
        : h("ol", { class: "flow" }, phase.stages.map(stageRow)))),
    h("section", { class: "phase" },
      h("h2", {}, "Now and then"),
      h("p", { class: "muted small" }, "Not part of the regular loop. Most of these are terminal commands the page doesn’t wrap.",
        h("span", { class: "no-hint" }, " Tick “Show commands” to see them.")),
      h("table", { class: "side" }, h("tbody", {}, SIDE_TASKS(o).map(([what, where, command, open]) =>
        h("tr", {},
          h("td", {}, what),
          h("td", {}, open
            ? h("button", { class: "quiet", onclick: () => { if (open[1] === "rav") wantRav = true; openStage({ open }); } },
                open[0] === "settings" ? "Open settings" : "Open applications")
            : file(where)),
          h("td", { class: "hint" }, command && cmd(command))))))),
  );
  // The check is asked for on every visit, so stage 6 says whether the setup
  // holds now, whether it was last checked here, in a terminal or never. The
  // server reuses its answer while nothing the check reads has changed.
  if (fresh) return;
  try {
    const result = await api("/api/doctor");
    const changed = JSON.stringify(result) !== JSON.stringify(lastCheck);
    lastCheck = result;
    if (changed && app.view === "workflow") renderWorkflow(main, true);
  } catch {
    // The stage keeps saying it could not be checked.
  }
}

// ── matches ────────────────────────────────────────────────────────────────

function scoreBar(label, value) {
  const fill = h("i", {});
  fill.style.width = `${value || 0}%`;
  return h("div", { class: "score" },
    h("div", { class: "cap" }, h("span", {}, label), h("strong", {}, value ?? "–")),
    h("div", { class: "bar" }, fill));
}

// Start a draft and, when it ends, open what it made. A draft can end two
// ways without failing: with documents, or with a placeholder because the
// posting's page could not be read. The command exits cleanly either way, so
// which it was is read off the application, not off the job.
async function draft(url, full) {
  // Checked here as well as on the server, so nobody is asked to confirm a
  // payment for an address that will then be refused.
  url = String(url || "").trim();
  if (!/^https?:\/\/\S+$/.test(url)) throw new Error("Paste the full web address of the posting, starting with https://");
  const before = new Map((await api("/api/applications")).applications.map((a) => [a.uid, a.status]));
  return start("draft", { url, full }, async (data) => {
    if (data.state !== "done") return app.refresh && app.refresh();
    const { applications } = await api("/api/applications");
    // The one this run made or moved on: new, or with a different status.
    const made = applications.find((a) => before.get(a.uid) !== a.status);
    if (made) {
      openApp = made.uid;
      pasteOpen = false;
      pasteText = "";
      if (made.status === "needs_jd") {
        job.note = "The posting could not be read, so nothing was drafted.";
        toast("jobradar could not read that posting. Paste its job description here.", true);
      }
    } else {
      job.note = "Nothing was drafted.";
    }
    renderJob();
    go("applications");
  });
}

function draftButtons(address) {
  const url = () => (typeof address === "function" ? address() : address);
  return [
    h("button", { class: "primary", disabled: busy(), title: "A tailored CV, plus a cover letter if the posting asks for one", onclick: act(() => draft(url(), false)) }, "Draft CV"),
    h("button", { disabled: busy(), title: "Always writes a cover letter too, and expects you to edit both before sending", onclick: act(() => draft(url(), true)) }, "CV + cover letter"),
  ];
}

// A posting you found yourself is drafted from the Applications page: it is
// the start of an application and never passes through a search, so it has
// no place among the matches. The box for its address is folded behind a
// button in that page's heading, so the list comes first; what was typed
// into it survives the page redrawing. `wantPaste` is set by the map's
// "Draft a posting you found": the page then opens with the box unfolded and
// the cursor in it.
let wantPaste = false;
let pasteOpen = false;
let pasteText = "";

// A write-up has three parts, by the prompt that writes it (search/writeup.py):
// why the posting fits, what to highlight when applying, and the concerns.
// Each starts with a paragraph that opens in bold. Deciding whether to apply
// takes the first and the last, so the card sets those side by side and folds
// the middle one, which is advice for after the decision. Returns null for
// text of any other shape, which is then shown as it is.
function writeupParts(html) {
  const box = h("div", { html });
  const parts = [];
  for (const node of [...box.children]) {
    const lead = node.tagName === "P" && node.firstChild && node.firstChild.nodeName === "STRONG" ? node.firstChild : null;
    if (!lead && !parts.length) return null;
    if (!lead) { parts[parts.length - 1].body.push(node); continue; }
    const part = { title: lead.textContent.trim().replace(/[.:]$/, ""), body: [] };
    parts.push(part);
    lead.remove();
    while (node.firstChild && (node.firstChild.nodeName === "BR" || (node.firstChild.nodeType === 3 && !node.firstChild.textContent.trim()))) node.firstChild.remove();
    if (!node.firstChild) continue; // the heading stood alone in its paragraph
    if (node.firstChild.nodeType === 3) node.firstChild.textContent = node.firstChild.textContent.trimStart();
    part.body.push(node);
  }
  return parts.length === 3 && parts.every((part) => part.body.length) ? parts : null;
}

// Which matches have "what to highlight" unfolded, by posting address.
const openAdvice = new Set();

function writeup(m) {
  const parts = writeupParts(m.writeup_html);
  if (!parts) return h("div", { class: "prose", html: m.writeup_html });
  const [why, advice, concerns] = parts;
  const column = (part) => h("section", {}, h("h4", {}, part.title), part.body);
  return h("div", { class: "prose writeup" },
    column(why), column(concerns),
    h("details", { open: openAdvice.has(m.url), ontoggle: (e) => (e.target.open ? openAdvice.add(m.url) : openAdvice.delete(m.url)) },
      h("summary", {}, advice.title), advice.body));
}

// Which matches are unfolded to their write-up, by posting address. Those of
// the newest report start open: they are what the page is opened for. Older
// ones stay one line each, so a week of reports can be read at a glance.
const openMatches = new Set();
let openedNewest = null;

function matchCard(m, practice, fold) {
  // The chevron is the fold control, in the place the folded row has its own,
  // and the whole heading can be clicked like the row can.
  return h("article", { class: "card match" },
    h("header", { class: "foldable", onclick: fold },
      h("button", { class: "caret", "aria-label": "Fold this match", "aria-expanded": "true" }, "▾"),
      h("div", { class: "grow" },
        h("h2", {}, m.title),
        h("div", { class: "muted" }, [m.company, m.location].filter(Boolean).join(" · "))),
      h("span", { class: `pill ${m.tier === "best" ? "best" : ""}` }, m.tier === "best" ? "Strong match" : "Okay match")),
    h("div", { class: "scores" }, scoreBar("Skill fit", m.skill_score), scoreBar("Interest fit", m.interest_score)),
    writeup(m),
    h("div", { class: "row gap" },
      external(m.url, "Open the posting"),
      h("span", { class: "grow" }),
      m.application
        ? h("button", { onclick: () => { openApp = m.application; go("applications"); } },
            `Open application #${m.application}${m.application_archived ? " (archived)" : ""}`)
        : !practice && draftButtons(m.url)));
}

function matchRow(m, unfold) {
  const status = m.application_status && STATUS[m.application_status];
  return h("button", { class: "match-row", "aria-label": `Unfold: ${m.title}`, "aria-expanded": "false", onclick: unfold },
    h("span", { class: "caret" }, "▸"),
    h("span", { class: "grow" }, h("strong", {}, m.title), h("span", { class: "muted" }, ` · ${m.company}`)),
    h("span", { class: "muted small" }, `skill ${m.skill_score ?? "–"} · interest ${m.interest_score ?? "–"}`),
    status && h("span", { class: `pill ${m.application_archived ? "grey" : status[1]}` },
      `${m.application_archived ? "Archived" : status[0]} #${m.application}`),
    h("span", { class: `pill ${m.tier === "best" ? "best" : ""}` }, m.tier === "best" ? "Strong" : "Okay"));
}

async function renderMatches(main) {
  app.refresh = () => renderMatches(main);
  let data;
  try {
    // The setup state is read again with the report: a step may have been
    // filled in, or emptied, in an editor since the page loaded.
    [data] = await Promise.all([api("/api/report"), loadState()]);
  } catch (error) {
    return put(main, h("div", { class: "note bad" }, error.message));
  }
  const ready = app.state.setup.ready;
  const run = data.last_run;
  const reports = data.reports;
  if (reports.length && openedNewest !== reports[0].date) {
    reports[0].matches.forEach((m) => openMatches.add(m.url));
    openedNewest = reports[0].date;
  }
  const total = reports.reduce((sum, report) => sum + report.matches.length, 0);
  const section = (report, i) => {
    // A practice run's report is the newest one; its postings are not real.
    const practice = i === 0 && run && run.practice && run.date === report.date;
    const count = report.matches.length;
    const set = (m, on) => () => { on ? openMatches.add(m.url) : openMatches.delete(m.url); renderMatches(main); };
    return h("section", { class: "report" },
      h("div", { class: "report-head" },
        h("h2", {}, report.date),
        h("span", { class: "muted" }, i === 0 ? "newest report" : "", count === 0 ? `${i === 0 ? " · " : ""}no match` : "", i < 0 ? " · archived" : ""),
        practice && h("span", { class: "pill warn" }, "Practice"),
        report.degraded && h("span", { class: "pill warn" }, "Incomplete")),
      practice && h("div", { class: "note warn" }, "This was a practice run on made-up sample postings. These are not real jobs; press Search now for real ones."),
      report.degraded && h("div", { class: "note warn" }, "This search was incomplete: a source failed, so matches may be missing."),
      count === 0 && i === 0 && h("p", { class: "muted" }, "Nothing worth your time in the latest search. That is a normal result; if it stays quiet for a week, lower the score floors or add companies under Settings."),
      report.matches.map((m) => (openMatches.has(m.url) ? matchCard(m, practice, set(m, false)) : matchRow(m, set(m, true)))));
  };
  put(main,
    crumbs("matches"),
    h("div", { class: "row between page-head" },
      h("h1", {}, "Matches"),
      // With no report yet, the card below is where the first search starts.
      (reports.length > 0 || !ready) && h("button", { title: !ready && LOCKED, disabled: busy() || !ready, onclick: act(runSearch) }, "Search now")),
    run && h("div", { class: "funnel" },
      h("span", {}, `Last search ${run.date}:`),
      h("span", {}, h("b", {}, (run.found ?? 0).toLocaleString()), " postings"), "→",
      h("span", {}, h("b", {}, run.new ?? 0), " new"), "→",
      h("span", {}, h("b", {}, run.passed ?? 0), " passed must-haves"), "→",
      h("span", {}, h("b", {}, run.scored ?? 0), " read in full"), "→",
      h("span", {}, h("b", {}, run.surfaced ?? 0), " worth your time")),
    reports.length === 0 && h("div", { class: "card empty" },
      h("h3", {}, "No report yet"),
      ready
        ? [h("p", { class: "muted" }, "A search reads the careers pages of your companies and the web, and lists here the few postings worth your time, each with two scores and the reasoning behind them. To see how that looks first, a practice run does the same with made-up postings for a few cents."),
           h("div", { class: "row" },
             h("button", { class: "primary", disabled: busy(), onclick: act(runSearch) }, "Search now"),
             h("button", { title: PRACTICE_TITLE, disabled: busy(), onclick: act(practiceRun) }, "Practice run"))]
        : setupFirst("Every search lists here the few postings worth your time, each with two scores and the reasoning behind them.")),
    reports.length > 1 && h("p", { class: "muted small" },
      `${total} match${total === 1 ? "" : "es"} in ${reports.length} reports not yet archived. Click a match’s heading to unfold or fold it.`,
      openMatches.size > 0
        ? h("button", { class: "quiet", onclick: () => { openMatches.clear(); renderMatches(main); } }, "Fold all")
        : h("button", { class: "quiet", onclick: () => { reports[0].matches.forEach((m) => openMatches.add(m.url)); renderMatches(main); } }, "Unfold the newest")),
    reports.map(section),
    reports.length > 0 && h("p", { class: "muted small gap" },
      "Reports leave this list when they are archived, on the window in config/retention.yaml.",
      data.hidden > 0 && ` ${data.hidden} older report${data.hidden > 1 ? "s are" : " is"} not shown: the page reads the newest ${reports.length} at most.`),
    archiveSection(reportArchive, {
      title: "Archived reports", count: data.archived, unit: "reports",
      body: (items) => items.map((report) => section(report, -1)),
      rerender: () => renderMatches(main),
    }),
    alt("Reports are ", file("reports/<date>.md"), ". Search: ", cmd(`${PY} -m jobradar.search.main`),
      " Draft: ", cmd(`${PY} -m jobradar.apply --url <address>`)),
  );
}

// ── applications ───────────────────────────────────────────────────────────

let openApp = null;
const STATUS = {
  needs_jd: ["Needs the job text", "warn"], drafted: ["Draft", "draft"], submitted: ["Submitted", "good"],
  rejected: ["Rejected", "grey"], withdrawn: ["Withdrawn", "grey"], offer: ["Offer", "best"],
};
const FILE_LABELS = [
  [/^notes\.md$/, "Notes and checklist"], [/^tailored_cv\.md$/, "Tailored CV"], [/^tailored_cv_(\w+)\.md$/, "Tailored CV ($1)"],
  [/^cover_letter\.md$/, "Cover letter"], [/^cover_letter_(\w+)\.md$/, "Cover letter ($1)"],
  [/^interview_prep\.md$/, "Interview prep"], [/^job_description\.md$/, "Job description"],
];
function fileLabel(name) {
  for (const [pattern, label] of FILE_LABELS) if (pattern.test(name)) return name.replace(pattern, label);
  return name;
}

// The list is grouped by what an application asks of you, in that order:
// drafts and unread postings need you, submitted ones wait on the employer,
// closed ones stay until the archive takes them. A group with nothing in it
// is left out.
const APP_GROUPS = [
  ["Needs you", ["needs_jd", "drafted"]],
  ["Awaiting an answer", ["submitted"]],
  ["Closed", ["rejected", "withdrawn", "offer"]],
];
// The day of the latest step an application took: the one its status names,
// or preparing an interview sheet, if that came later. Editing its documents
// doesn't move it.
function lastUpdate(a) {
  let step;
  if (a.status === "submitted") step = a.submitted_on || a.prepared_on || "";
  else if (a.status === "needs_jd" || a.status === "drafted") step = a.prepared_on || "";
  else step = a.closed_on || a.submitted_on || a.prepared_on || "";
  const prepped = a.prepped_on || "";
  return prepped > step ? prepped : step;
}

async function renderApplications(main) {
  app.refresh = () => renderApplications(main);
  const [{ applications, archived }] = await Promise.all([api("/api/applications"), loadState()]);
  if (openApp) {
    const found = applications.find((a) => Number(a.uid) === Number(openApp))
      // Not active: an archived one, from a match link or an address.
      || await api(`/api/application?uid=${encodeURIComponent(openApp)}`).catch(() => null);
    if (found) return renderApplication(main, found);
    openApp = null;
  }
  const ready = app.state.setup.ready;
  const empty = applications.length === 0;
  if (wantPaste) pasteOpen = true;
  const paste = h("input", { type: "url", placeholder: "https://…", class: "grow", value: pasteText, oninput: (e) => { pasteText = e.target.value; } });
  const togglePaste = () => { pasteOpen = !pasteOpen; wantPaste = pasteOpen; renderApplications(main); };
  // Newest step first within a group; a status no group names (there is none
  // today) lands in the last one, so nothing can drop off the list.
  const grouped = new Set(APP_GROUPS.flatMap(([, statuses]) => statuses));
  const groups = APP_GROUPS
    .map(([name, statuses], i) => [name, applications
      .filter((a) => statuses.includes(a.status) || (i === APP_GROUPS.length - 1 && !grouped.has(a.status)))
      .sort((a, b) => lastUpdate(b).localeCompare(lastUpdate(a)) || Number(b.uid) - Number(a.uid))])
    .filter(([, rows]) => rows.length);
  put(main,
    crumbs("applications"),
    h("div", { class: "row between page-head" },
      h("h1", {}, "Applications"),
      ready && !empty && h("button", { "aria-expanded": String(pasteOpen), title: "Paste the address of a posting the search didn’t find", onclick: togglePaste }, "Draft a posting you found")),
    empty && h("div", { class: "card empty" },
      h("h3", {}, "Nothing drafted yet"),
      ready
        ? [h("p", { class: "muted" }, "An application starts as a draft written for one posting: a tailored CV, a cover letter when one is asked for, and notes on fit and gaps. You edit it here, send it yourself, and record what came back. Draft one from a match, or from a posting you found yourself."),
           h("div", { class: "row" }, h("button", { onclick: () => go("matches") }, "Draft from a match →"))]
        : setupFirst("An application starts as a draft written for one posting: a tailored CV, a cover letter when one is asked for, and notes on fit and gaps.")),
    // With nothing drafted yet the box is simply there, under the card that
    // says what an application is: it is one of the two ways to the first.
    ready && (pasteOpen || empty) && h("div", { class: "card", id: "own-posting" },
      h("div", { class: "row between" },
        h("h3", {}, "Found a posting yourself?"),
        !empty && h("button", { class: "quiet", onclick: togglePaste }, "Close")),
      h("p", { class: "muted small" }, "Paste its web address and jobradar scores it and drafts a tailored CV for it, the same as for a match."),
      h("div", { class: "row" }, paste, draftButtons(() => paste.value))),
    !empty && h("div", { class: "card" }, h("table", { class: "apps" },
      h("thead", {}, h("tr", {}, ["#", "Role", "Status", "Last update"].map((t) => h("th", {}, t)))),
      groups.map(([name, rows]) => h("tbody", {},
        h("tr", { class: "group" }, h("th", { colSpan: 4, scope: "rowgroup" }, name, h("span", { class: "muted" }, rows.length))),
        rows.map((a) =>
          h("tr", { class: "clickable", onclick: () => { openApp = a.uid; go("applications"); } },
            h("td", { class: "muted" }, a.uid),
            h("td", {}, h("strong", {}, a.title), h("div", { class: "muted small" }, a.company)),
            h("td", {}, h("span", { class: `pill ${STATUS[a.status]?.[1] ?? ""}` }, STATUS[a.status]?.[0] ?? a.status)),
            h("td", { class: "small date" }, lastUpdate(a)))))))),
    h("div", { id: "rav" }),
    archiveSection(applicationArchive, {
      title: "Archived applications", count: archived, unit: "applications",
      body: (items) => h("div", { class: "card" }, h("table", { class: "apps" },
        h("thead", {}, h("tr", {}, ["#", "Role", "Status", "Submitted", "Archived"].map((t) => h("th", {}, t)))),
        h("tbody", {}, items.map((a) =>
          h("tr", { class: "clickable", onclick: () => { openApp = a.uid; go("applications"); } },
            h("td", { class: "muted" }, a.uid),
            h("td", {}, h("strong", {}, a.title), h("div", { class: "muted small" }, a.company)),
            h("td", {}, h("span", { class: "pill grey" }, STATUS[a.status]?.[0] ?? a.status)),
            h("td", { class: "small date" }, a.submitted_on || ""),
            h("td", { class: "small date" }, a.archived_on || "")))))),
      rerender: () => renderApplications(main),
    }),
  );
  renderRav(main.querySelector("#rav"));
  if (wantPaste) {
    wantPaste = false;
    paste.scrollIntoView({ block: "center" });
    paste.focus();
  }
}

// The RAV proof for one month, and filing it. Shown only with RAV switched on
// (Settings → How much you see). Filing runs `--rav-filed`, which saves the
// table to reports/rav/, records the month and runs the archive sweep: an
// application kept only for the proof of a month can leave once it is filed.
async function renderRav(box, month) {
  let r;
  try {
    r = await api(`/api/rav${month ? `?month=${month}` : ""}`);
  } catch (error) {
    return put(box, h("div", { class: "note bad" }, error.message));
  }
  if (!r.enabled) return put(box);
  const picker = h("input", { type: "month", value: r.month, max: r.current, onchange: (e) => renderRav(box, e.target.value) });
  const output = h("div", {});
  const ongoing = r.month === r.current;
  const file = act(async () => {
    const ok = await ask(`Mark ${r.month} as filed?`, [
      h("p", {}, "Do this after you have handed this month’s proof in to the RAV. jobradar saves the table below to ",
        h("code", {}, `reports/rav/${r.month}.md`), " and records the month as filed."),
      h("p", {}, "Applications that were kept only for this month’s proof, and are otherwise due for the archive, move there now."),
      ongoing && h("p", { class: "note warn" }, "This month is not over: applications you submit later this month will not be in the saved table."),
    ], "Mark as filed");
    if (!ok) return;
    const result = await api("/api/run", { action: "rav_filed", month: r.month });
    if (!result.ok) return put(output, h("pre", { class: "out" }, result.output));
    toast(`${r.month} recorded as filed.`);
    await renderRav(box, r.month);
    const archived = (result.output || "").split("\n").filter((line) => line.startsWith("Archived"));
    if (archived.length) put(box.querySelector(".rav-output"), h("div", { class: "note" }, `${archived.length} application${archived.length > 1 ? "s" : ""} moved to the archive.`));
  });
  put(box, h("div", { class: "card rav", id: "rav-card" },
    h("div", { class: "row between" },
      h("h3", {}, "RAV proof of applications"),
      r.unfiled.length > 0
        ? h("span", { class: "pill act" }, `Not filed: ${r.unfiled.join(", ")}`)
        : r.filed.length > 0 && h("span", { class: "pill good" }, "All past months filed")),
    h("div", { class: "row gap" },
      picker,
      r.filed_on
        ? h("span", { class: "pill good" }, `Filed on ${r.filed_on}`)
        : h("span", { class: `pill ${ongoing ? "grey" : "act"}` }, ongoing ? "This month, ongoing" : "Not filed"),
      h("span", { class: "muted small grow" }, `${r.submitted} submitted`),
      !r.filed_on && h("button", { class: "primary", onclick: file }, "Mark as filed")),
    h("div", { class: "rav-output" }, output),
    h("div", { class: "prose rav-table" , html: r.table_html }),
    r.report && h("p", { class: "muted small" }, "Saved when it was filed: ", h("code", {}, r.report)),
    alt("Show a month: ", cmd(`${PY} -m jobradar.apply --rav ${r.month}`), " File it: ", cmd(`${PY} -m jobradar.apply --rav-filed ${r.month}`))));
  if (wantRav) {
    wantRav = false;
    box.scrollIntoView({ block: "start" });
  }
}

// Set by the map's "File a RAV month": Applications opens at the RAV card.
let wantRav = false;

// For a posting the pipeline could not read: paste its text, then draft.
// Nothing is paid for until the text is long enough for the pipeline to use,
// which is checked here and again on the server.
function pasteBox(a, again) {
  // While a draft runs, what was pasted is what it reads: shown, not editable.
  const area = h("textarea", { class: "tall", value: a.jd_text || "", readOnly: busyWith(a), placeholder: "Paste the whole job description here: tasks, requirements, what it says about the company." });
  const count = h("span", { class: "muted small" });
  const size = () => area.value.trim().length;
  const recount = () => {
    const n = size();
    count.textContent = n >= a.jd_min ? `${n.toLocaleString()} characters` : `${n} characters; at least ${a.jd_min} are needed`;
  };
  area.addEventListener("input", recount);
  recount();
  const save = act(async () => {
    if (size() < a.jd_min) throw new Error(`That is too short to be a job description: ${size()} characters, and at least ${a.jd_min} are needed.`);
    await api("/api/jd", { uid: a.uid, text: area.value });
    // The text is stored whatever happens next, so declining the payment
    // doesn't lose the paste.
    if (!(await draft(a.url, a.tier === "full"))) again();
  });
  return h("div", { class: "card" },
    h("h3", {}, "jobradar could not read this posting"),
    h("p", {}, a.jd_why ? `The reason given: ${a.jd_why}. ` : "", "Some career sites only show a posting to a browser. Open it, copy the job description, and paste it below; the draft is then written from your copy."),
    h("p", {}, external(a.url, "Open the posting")),
    a.jd_ready && h("div", { class: "note" }, "A job description is already stored for this posting. Change it below if you like, then draft."),
    area,
    h("div", { class: "row between gap" }, count,
      h("button", { class: "primary", disabled: busy(), onclick: save }, "Save and draft")),
    alt("Or paste it under the marker line in ", file(`applications/${a.folder}/job_description.md`), " and run ", cmd(`${PY} -m jobradar.apply --url '${a.url}'`)));
}

async function renderApplication(main, a) {
  const again = () => renderApplications(main);
  const viewer = h("div", {});
  const output = h("div", {});
  const quick = (action, question) => act(async () => {
    if (question && !(await ask(question[0], h("p", {}, question[1]), question[2]))) return;
    const result = await api("/api/run", { action, uid: a.uid });
    if (!result.ok) return put(output, h("pre", { class: "out" }, result.output));
    toast("Recorded.");
    again();
  });
  const long = (action) => act(async () => { await start(action, { uid: a.uid }, again); });
  // The check reports in the bar at the bottom, as the other commands do.
  // It exits cleanly with warnings, and with an error when it finds a
  // mistake; either way its findings are what Details shows, unfolded.
  const lint = act(async () => {
    put(output);
    await start("lint", { uid: a.uid }, async (data) => {
      const clean = job.lines.includes("No issues found.");
      if (data.state === "done" && clean) {
        job.note = "No problems found.";
      } else if (data.state === "done") {
        job.verdict = ["Passes, with warnings", "warn"];
        job.note = "Worth a look before you send it: see the list below.";
        job.open = true;
      } else if (data.state === "failed") {
        job.verdict = ["Problems found", "warn"];
        job.note = "Have a look at these before you send it: see the list below.";
        job.open = true;
      }
      renderJob();
      again();
    });
  });

  // The documents are tabs, the page's way of switching what is shown, so
  // they can't be taken for the action buttons above. A PDF (or the .html
  // print version) is not a tab of its own: it is offered as a link inside
  // the tab of the document it was made from.
  const texts = a.files.filter((name) => name.endsWith(".md"));
  const printed = (name) => a.files.filter((other) => !other.endsWith(".md") && other.replace(/\.\w+$/, ".md") === name);
  const loose = a.files.filter((name) => !name.endsWith(".md") && !texts.includes(name.replace(/\.\w+$/, ".md")));
  const printLink = (name) => h("a", { class: "print-link", href: fileUrl(a.uid, name), target: "_blank", rel: "noopener" },
    `Open the ${name.split(".").pop().toUpperCase()} ↗`);

  const show = async (name) => {
    tabs.forEach(([n, b]) => b.classList.toggle("on", n === name));
    const file = await api(`/api/file?uid=${a.uid}&name=${encodeURIComponent(name)}`);
    // A document that is sent as a PDF is saved together with its PDF, so
    // what you read here and what you send never differ. Making the PDF runs
    // `--pdf`, which checks the application first and refuses on a mistake
    // (a wrong company name, say): then the text is saved, the PDF is not,
    // and the job bar's Details say why.
    const edit = () => {
      const area = h("textarea", { class: "tall", value: file.text });
      const save = act(async () => {
        await api("/api/file", { uid: a.uid, name, text: area.value });
        if (!file.makes_pdf) {
          toast("Saved.");
          return show(name);
        }
        await start("pdf", { uid: a.uid }, async (data) => {
          // Judged by the PDF itself, not the command's exit: a print can
          // fall back to HTML and still end cleanly.
          const after = await api(`/api/file?uid=${a.uid}&name=${encodeURIComponent(name)}`).catch(() => null);
          if (after && !after.pdf_problem) toast("Saved, and the PDF is made again.");
          else if (data.state === "failed") toast("Saved, but the PDF was not made: the check found a problem. See Details below.", true);
          else toast("Saved, but the PDF could not be made. See the note above the text.", true);
          openApp = a.uid;
          go("applications"); // the PDF link may be new
        });
      });
      put(viewer, area, h("div", { class: "row end gap" },
        h("button", { onclick: () => show(name) }, "Cancel"),
        h("button", { class: "primary", disabled: file.makes_pdf && busy(), onclick: save },
          file.makes_pdf ? "Save and make the PDF" : "Save")));
    };
    // The document's own tools (its PDF, Edit) sit at the right end of the
    // tab row, so a document without any starts right under the tabs.
    put(tools, printed(name).map(printLink), loose.map(printLink),
      file.editable && h("button", { class: "small", onclick: () => { put(tools, loose.map(printLink)); edit(); } }, "Edit"));
    put(viewer,
      file.pdf_problem && h("div", { class: "note warn" }, file.pdf_problem),
      h("div", { class: "prose", html: file.html }));
  };
  const tools = h("div", { class: "row doc-tools" });
  const tabs = texts.map((name) => [name, h("button", { role: "tab", onclick: act(() => show(name)) }, fileLabel(name))]);

  // An archived application is the record of what was sent: read, not changed.
  const waiting = a.status === "needs_jd" && !a.archived;
  // A command working on this application (its draft, say): its status
  // can't be changed meanwhile. Stop, in the bar below, is the way out.
  const working = busyWith(a);
  const id = String(Number(a.uid));
  // Two kinds of action, in two labelled rows: work on the documents, and
  // recording what happened to the application. An archived one has neither.
  const work = [];
  const record = [];
  if (!a.archived && a.status === "drafted") {
    work.push(
      h("button", { disabled: busy(), onclick: lint }, "Check before sending"),
      h("button", { disabled: busy(), onclick: long("pdf") }, "Make PDFs"),
      a.tier !== "full" && h("button", { disabled: busy(), onclick: long("upgrade") }, "Add a cover letter"));
    record.push(h("button", { class: "primary", onclick: quick("submitted", ["Mark as submitted?", "Do this after you have actually submitted the application on the employer’s site. jobradar never sends anything itself.", "Mark as submitted"]) }, "Submitted"));
  }
  if (!a.archived && (a.status === "drafted" || a.status === "submitted")) {
    work.push(h("button", { disabled: busy(), onclick: long("prep") }, "Prepare for an interview"));
  }
  if (!a.archived && a.status === "submitted") {
    record.push(
      h("button", { onclick: quick("offer", ["Record an offer?", "Congratulations. This closes the application.", "Record offer"]) }, "Got an offer"),
      h("button", { onclick: quick("rejected", ["Record a rejection?", "This closes the application.", "Record rejection"]) }, "Rejected"));
  }
  if (a.open) record.push(h("button", { onclick: quick("withdrawn", ["Withdraw?", "This closes the application as withdrawn.", "Withdraw"]) }, "Withdrawn"));
  if (working) record.length = 0;
  const group = (label, buttons) => buttons.filter(Boolean).length > 0 &&
    h("div", { class: "action-group" }, h("span", { class: "action-label" }, label), h("div", { class: "row" }, buttons));

  put(main, 
    crumbs("applications", a.archived ? `#${a.uid} (archived)` : `#${a.uid}`),
    h("div", { class: "row between app-head" },
      // The posting link belongs with what identifies the application, so
      // the box below holds actions only and is left out when there are none.
      h("div", {}, h("h1", {}, a.title), h("p", { class: "muted" }, [
        a.company, `#${a.uid}`, a.tier === "full" ? "CV + cover letter" : "quick draft",
        a.skill_score != null && `skill fit ${a.skill_score}`, a.interest_score != null && `interest fit ${a.interest_score}`,
      ].filter(Boolean).join(" · "), " · ", external(a.url, "posting ↗"))),
      h("div", { class: "row" },
        a.archived && h("span", { class: "pill grey" }, `Archived ${a.archived_on || ""}`.trim()),
        h("span", { class: `pill ${a.archived ? "grey" : STATUS[a.status]?.[1] ?? ""}` }, STATUS[a.status]?.[0] ?? a.status))),
    a.archived && h("div", { class: "note" }, "This application is archived: it is shown as it was sent and can't be changed here. Its material is in ",
      h("code", {}, `applications/archive/${a.folder}/`), ", to reuse for a new application."),
    waiting && pasteBox(a, again),
    (work.some(Boolean) || record.length > 0) && h("div", { class: "card" },
      group("Work on it", work),
      group("Update status", record),
      output,
      !waiting && !a.archived && h("details", { class: "gap hint" },
        h("summary", {}, "The same in the terminal"),
        alt("The folder is ", file(`applications/${a.folder}/`), ". Edit the Markdown files there in your editor."),
        h("div", { class: "cmds" },
          [`--lint ${id}`, `--pdf ${id}`, `--upgrade ${id}`, `--mark-submitted ${id}`, `--prep ${id}`,
           `--mark-rejected ${id}`, `--mark-offer ${id}`, `--mark-withdrawn ${id}`]
            .map((args) => cmd(`${PY} -m jobradar.apply ${args}`))))),
    // A placeholder's only file is the stub the paste box above writes to;
    // showing it as a document would offer a second, rawer way to do the same.
    !waiting && h("div", { class: "card" },
      h("div", { class: "tabs doc-tabs" },
        h("div", { class: "row", role: "tablist" }, tabs.map(([, b]) => b)),
        h("span", { class: "grow" }), tools),
      viewer),
  );
  const first = !waiting && texts[0];
  if (first) show(first).catch((error) => toast(error.message, true));
}

// ── settings ───────────────────────────────────────────────────────────────

// The cost warnings come from the server (config.cost_warnings, the same
// rules the setup check applies to search.yaml), asked again as the values
// change. Each is shown beside the field it is about; one that is about the
// combination goes under the per-search line.
async function stepSearch(box, done) {
  const s = await api("/api/search");
  const number = (value, min, max) => h("input", { type: "number", min, max, value });
  const skill = number(s.min_skill, 0, 100), interest = number(s.min_interest, 0, 100), best = number(s.best_threshold, 0, 100);
  const maxBest = number(s.max_best, 1, 5), maxOkay = number(s.max_okay, 0, 10);
  const rav = h("input", { type: "checkbox", checked: s.rav });
  const slots = { min_skill: h("div", {}), best_threshold: h("div", {}), per_run: h("div", {}), combined: h("div", {}) };
  const values = () => ({
    min_skill: skill.value, min_interest: interest.value, best_threshold: best.value,
    max_best: maxBest.value, max_okay: maxOkay.value,
  });
  let current = [];
  let asked = 0;
  const check = async () => {
    const ticket = ++asked;
    const { warnings } = await api(`/api/search_warnings?${new URLSearchParams(values())}`).catch(() => ({ warnings: [] }));
    if (ticket !== asked) return; // an older answer arriving late
    current = warnings;
    for (const [key, slot] of Object.entries(slots)) {
      const mine = warnings.filter((w) => (key === "per_run" ? ["per_run", "max_best", "max_okay"].includes(w.key) : w.key === key));
      put(slot, mine.map((w) => h("div", { class: "note warn small" }, w.message)));
    }
  };
  [skill, interest, best, maxBest, maxOkay].forEach((input) => input.addEventListener("input", check));
  const save = act(async () => {
    // A value the form refuses is reported before asking about cost: never
    // ask "save anyway?" for something that will not be saved.
    const invalid = [skill, interest, best, maxBest, maxOkay].find((input) => !input.checkValidity());
    if (invalid) {
      invalid.reportValidity();
      throw new Error(`That value must be between ${invalid.min} and ${invalid.max}.`);
    }
    if (current.length && !(await ask("Save settings that raise the cost of a run?",
      [h("p", {}, "These settings make each search spend more, mostly on write-ups of postings that are not worth reading:"),
       h("ul", {}, current.map((w) => h("li", {}, w.message)))],
      "Save anyway"))) return;
    await api("/api/search", { ...values(), rav: rav.checked });
    toast("Saved.");
    done();
  });
  put(box,
    h("h1", {}, "How much you see"),
    h("p", { class: "muted" }, "Every posting gets two scores from 0 to 100. These decide which ones reach you, and so what a search costs: every match shown is written up, about 3 cents each."),
    h("div", { class: "card" },
      field("Skill floor", "Postings scoring lower on skill fit are dropped for good. Lower it if you see too little, raise it if what you see isn’t worth opening. Usual: 60.", skill),
      slots.min_skill,
      field("Interest floor", "The same for interest fit. 0 switches it off, so a job you are qualified for but lukewarm about still shows. Usual: 0 to 40.", interest),
      field("Bar for a “strong match”", "Applied to the average of both scores. Usual: 80.", best),
      slots.best_threshold,
      h("div", { class: "row" }, "Show at most", maxBest, "strong (1 to 5) and", maxOkay, "okay (0 to 10) matches per search"),
      slots.per_run,
      slots.combined),
    h("div", { class: "card" },
      h("label", { class: "check" }, rav, h("span", {}, h("strong", {}, "I am registered with a RAV. "), "Adds the monthly proof-of-applications table to the Applications page."))),
    h("div", { class: "row end" }, h("button", { class: "primary", onclick: save }, "Save")),
  );
  check();
}

async function stepHealth(box, _done, fresh) {
  put(box, h("p", { class: "muted" }, "Checking…"));
  const { list, failed } = await checksList(fresh);
  // Setup done but never searched: the check is the last stop before the
  // first search, so it says so and leads there.
  const first = !failed && app.state && app.state.setup.ready && !app.state.setup.has_run;
  put(box, h("h1", {}, "Setup check"), h("div", { class: "card" }, list,
    h("div", { class: `note ${failed ? "bad" : ""}` }, failed
      ? `${failed} thing${failed > 1 ? "s" : ""} to fix before a search can run.`
      : first ? "Setup is complete and everything a search needs is in place. Next: your first search, from the workflow page."
      : "Everything a search needs is in place."),
    h("div", { class: "row end" },
      h("button", { onclick: () => stepHealth(box, _done, true) }, "Check again"),
      first && h("button", { onclick: () => go("workflow") }, "Go to your first search →"))));
}

// The tabs follow the map: the setup steps in their order, under the titles
// the map gives them, then the check. "How much you see" is not a setup step
// (it ships with usable values), so it comes last and stands apart.
const SETTINGS = [
  ["key", "Connect Claude", stepKey], ["cv", "Your CV", stepCv], ["identity", "What you want", stepIdentity],
  ["constraints", "Must-haves", stepConstraints], ["boards", "Where to search", stepBoards],
  ["health", "Check", stepHealth], ["search", "How much you see", stepSearch],
];
const SETTING_TITLES = Object.fromEntries(SETTINGS.map(([id, label]) => [id, label]));
// null until Settings is first opened or a link names a tab: see render().
let settingsTab = null;

// What each settings tab writes, for doing the same by hand.
const BY_HAND = {
  search: () => ["Edit ", file("config/search.yaml"), "; every key is explained there and in ", file("docs/SEARCH.md"), ". What the program reads: ", cmd(`${PY} -m jobradar.config`)],
  boards: () => ["List the names in ", file("config/companies.yaml"), ", as ", file("config/boards/"), " spells them. A board the catalogue lacks goes there with its own ats and slug."],
  constraints: () => ["Edit ", file("config/constraints.yaml"), ". It also takes cities and countries, which this form leaves as they are."],
  identity: () => ["Edit ", file("profile/identity.md"), ". Any headings work; the whole page is read."],
  cv: () => ["Put ", file(".md"), " or ", file(".docx"), " files in ", file("profile/cvs/"), ". The file name is the CV’s label in reports."],
  key: () => ["Put ", file("ANTHROPIC_API_KEY=sk-ant-…"), " in ", file(".env"), ", or run ", cmd("ant auth login"), " and leave the key out."],
  health: () => [cmd(`${PY} -m jobradar.doctor`)],
};
const SAVES_IN_PLACE = new Set(["search", "boards", "constraints"]);

function renderSettings(main) {
  const body = h("div", {});
  const steps = setupSteps();
  const open = firstOpenStep();
  const wasGuiding = guiding();
  // While setup is still open, saving a step leads straight to the next open
  // one, and the last to the check; afterwards a save stays where it is.
  const saved = async () => {
    await loadState().catch(() => {});
    if (!wasGuiding) return renderSettings(main);
    const next = firstOpenStep();
    settingsTab = next || "health";
    if (next) toast(`Saved. Next: ${SETTING_TITLES[next]}.`);
    go("settings");
    window.scrollTo(0, 0);
  };
  // A step's place and state, in the colours of the map's rail: green and a
  // tick when done, orange for the one to do next. Shown only while setup is
  // open; once it is done the tabs are plain names again.
  const marker = (id) => {
    const place = SETUP_ORDER.indexOf(id);
    if (!wasGuiding || place < 0) return null;
    const state = steps[id] ? "done" : id === open ? "act" : "";
    return h("span", { class: `tab-num ${state}`, title: steps[id] ? "Done" : id === open ? "Next" : "To do" }, steps[id] ? "✓" : place + 1);
  };
  const done = SETUP_ORDER.filter((id) => steps[id]).length;
  put(main,
    crumbs("settings"),
    wasGuiding && h("p", { class: "muted small setup-progress" },
      h("strong", {}, `Setup: ${done} of ${SETUP_ORDER.length} steps done. `),
      "Fill them in from left to right; a search can run once all five are done."),
    // While setup is open the bar holds the steps and their check; "How much
    // you see" joins it once they are done, or when a link opens it.
    h("div", { class: "tabs settings-tabs" }, SETTINGS.filter(([id]) => !wasGuiding || id !== "search" || settingsTab === id).map(([id, label]) =>
      h("button", { class: `${settingsTab === id ? "on" : ""} ${id === "search" ? "apart" : ""}`, onclick: () => { settingsTab = id; go("settings"); } },
        marker(id), label))),
    body,
    alt(BY_HAND[settingsTab](),
      SAVES_IN_PLACE.has(settingsTab) && h("div", { class: "muted" }, "Saving here changes only the lines that differ. Your comments and the keys this form doesn’t show stay as you wrote them.")));
  const fn = SETTINGS.find(([id]) => id === settingsTab)[2];
  fn(body, saved).catch((error) => put(body, h("div", { class: "note bad" }, error.message)));
}

// ── start ──────────────────────────────────────────────────────────────────

(async function init() {
  try {
    const state = await loadState();
    $("home").addEventListener("click", () => go("workflow"));
    initHints();
    recall();
    if (state.job && state.job.state === "running") { job.data = state.job; pollJob(); }
    render();
  } catch (error) {
    $("main").replaceChildren(h("div", { class: "note bad" }, error.message));
  }
})();
