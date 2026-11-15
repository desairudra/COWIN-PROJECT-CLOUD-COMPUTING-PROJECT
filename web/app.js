// Frontend for the IT457 CoWIN registration model.
// Talks to the registration service (service/app/main.py).
"use strict";

const API = (window.COWIN_API_BASE || "").replace(/\/$/, "");
const MAX = 4;
const ID_LABELS = {
  AADHAAR: "Aadhaar", DRIVING_LICENSE: "Driving licence", PAN: "PAN", PASSPORT: "Passport",
  PENSION_PASSBOOK: "Pension passbook", NPR_SMART_CARD: "NPR card", VOTER_ID: "Voter ID",
  UDID: "UDID", RATION_CARD: "Ration card",
};
const GENDER = { FEMALE: "female", MALE: "male", OTHER: "other" };

const $ = (id) => document.getElementById(id);
const state = { mobile: null, txnId: null, token: null, otpExpires: 0, timer: null, pendingKey: null };

// ---------- storage (session only; a page refresh keeps you signed in) -------
const store = {
  save() {
    try { sessionStorage.setItem("cowin.session", JSON.stringify({ mobile: state.mobile, token: state.token })); } catch (_) { }
  },
  load() {
    try { return JSON.parse(sessionStorage.getItem("cowin.session") || "null"); } catch (_) { return null; }
  },
  clear() {
    try { sessionStorage.removeItem("cowin.session"); } catch (_) { }
  },
};

// ---------- API -----------------------------------------------------------------
class NetworkError extends Error { }

async function api(path, { method = "GET", body, headers = {} } = {}) {
  const h = { ...headers };
  if (body !== undefined) h["Content-Type"] = "application/json";
  if (state.token) h["Authorization"] = `Bearer ${state.token}`;
  let res;
  try {
    res = await fetch(API + path, { method, headers: h, body: body === undefined ? undefined : JSON.stringify(body) });
  } catch (e) {
    throw new NetworkError("network");
  }
  const servedBy = res.headers.get("X-Served-By");
  if (servedBy) $("served-by").textContent = `Answered by replica ${servedBy}`;
  let data = null;
  try { data = await res.json(); } catch (_) { }
  return { status: res.status, ok: res.ok, data, headers: res.headers };
}

function errorText(r) {
  const e = r.data && r.data.error;
  if (!e) return `The server returned an unexpected response (HTTP ${r.status}). Try again.`;
  if (e.details && e.details.length) {
    return e.details.map((d) => `${fieldName(d.field)}: ${d.message}`).join(". ") + ".";
  }
  return e.message;
}

function fieldName(f) {
  return ({
    mobile: "Mobile number", otp: "Code", full_name: "Name", birth_year: "Year of birth",
    gender: "Gender", photo_id_type: "Photo ID", photo_id_number: "ID number"
  })[f] || f;
}

function newKey() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  const b = new Uint8Array(16);
  crypto.getRandomValues(b);
  return Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
}

// ---------- UI helpers ------------------------------------------------------------
function show(view) {
  for (const v of ["mobile", "otp", "home"]) $(`view-${v}`).hidden = v !== view;
  const order = ["mobile", "otp", "home"];
  document.querySelectorAll(".steps li").forEach((li) => {
    const i = order.indexOf(li.dataset.step), cur = order.indexOf(view);
    li.classList.toggle("done", i < cur);
    li.classList.toggle("current", i === cur);
  });
  const first = $(`view-${view}`).querySelector("input:not([type=radio]), select");
  if (first) first.focus();
}

function setError(id, msg, input) {
  const el = $(id);
  el.textContent = msg || "";
  el.hidden = !msg;
  if (input) input.setAttribute("aria-invalid", msg ? "true" : "false");
}

function busy(form, on) {
  form.querySelectorAll("button").forEach((b) => (b.disabled = on));
}

let toastTimer;
function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), 4500);
}

const masked = (m) => `${m.slice(0, 2)}XXXXXX${m.slice(-2)}`;

// ---------- 1. mobile ----------------------------------------------------------------
async function requestOtp(mobile) {
  const r = await api("/api/v1/otp/request", { method: "POST", body: { mobile } });
  if (!r.ok) return errorText(r);
  state.mobile = mobile;
  state.txnId = r.data.txn_id;
  state.otpExpires = Date.now() + r.data.expires_in * 1000;
  $("otp-mobile").textContent = `+91 ${mobile.slice(0, 5)} ${mobile.slice(5)}`;
  $("demo-otp").hidden = !r.data.demo_otp;
  $("demo-otp-value").textContent = r.data.demo_otp || "";
  $("otp").value = "";
  setError("otp-error", "", $("otp"));
  startTimer();
  return null;
}

$("form-mobile").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("mobile");
  const mobile = input.value.replace(/\D/g, "");
  if (!/^[6-9]\d{9}$/.test(mobile)) {
    setError("mobile-error", "Enter a 10-digit mobile number that starts with 6, 7, 8 or 9.", input);
    return;
  }
  setError("mobile-error", "", input);
  busy(e.target, true);
  try {
    const err = await requestOtp(mobile);
    if (err) setError("mobile-error", err, input);
    else show("otp");
  } catch (_) {
    setError("mobile-error", "Couldn't reach the server. Check your connection and try again.", input);
  } finally {
    busy(e.target, false);
  }
});

// ---------- 2. OTP ----------------------------------------------------------------------
function startTimer() {
  clearInterval(state.timer);
  const tick = () => {
    const left = Math.max(0, Math.round((state.otpExpires - Date.now()) / 1000));
    $("otp-timer").textContent = left
      ? `Code expires in ${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}.`
      : "This code has expired. Send a new code.";
    if (!left) clearInterval(state.timer);
  };
  tick();
  state.timer = setInterval(tick, 1000);
}

$("form-otp").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("otp");
  const otp = input.value.trim();
  if (!/^\d{4,8}$/.test(otp)) {
    setError("otp-error", "Enter the code using digits only.", input);
    return;
  }
  busy(e.target, true);
  try {
    const r = await api("/api/v1/otp/verify", { method: "POST", body: { txn_id: state.txnId, otp } });
    if (!r.ok) {
      setError("otp-error", errorText(r), input);
      return;
    }
    clearInterval(state.timer);
    state.token = r.data.token;
    store.save();
    await openHome(r.data.new_registration ? "Mobile number confirmed. You can now add people." : "Signed in.");
  } catch (_) {
    setError("otp-error", "Couldn't reach the server. Try again.", input);
  } finally {
    busy(e.target, false);
  }
});

$("resend-otp").addEventListener("click", async () => {
  try {
    const err = await requestOtp(state.mobile);
    if (err) setError("otp-error", err, $("otp"));
    else toast("New code sent.");
  } catch (_) {
    setError("otp-error", "Couldn't reach the server. Try again.", $("otp"));
  }
});

$("change-mobile").addEventListener("click", () => {
  clearInterval(state.timer);
  show("mobile");
});

// ---------- 3. household ------------------------------------------------------------
function renderSlots(people, freshId) {
  const ol = $("slots");
  ol.innerHTML = "";
  for (let i = 0; i < MAX; i++) {
    const p = people[i];
    const li = document.createElement("li");
    li.className = "slot" + (p ? " filled" : "") + (p && p.beneficiary_id === freshId ? " fresh" : "");
    if (p) {
      const who = document.createElement("span");
      who.className = "who";
      who.textContent = p.full_name;
      const meta = document.createElement("span");
      meta.className = "meta";
      meta.textContent = `Born ${p.birth_year}, ${GENDER[p.gender]}`;
      const id = document.createElement("span");
      id.className = "idno";
      id.textContent = `${ID_LABELS[p.photo_id_type]} ending ${p.photo_id_masked.slice(-4)}`;
      li.append(who, meta, id);
    } else {
      li.textContent = "Open slot";
    }
    ol.append(li);
  }
  const left = MAX - people.length;
  $("slots-caption").textContent = left
    ? `${people.length} of ${MAX} used. You can add ${left} more ${left === 1 ? "person" : "people"}.`
    : `${MAX} of ${MAX} used.`;
  $("form-person").hidden = left === 0;
  $("full-note").hidden = left !== 0;
}

async function refresh(freshId) {
  const r = await api("/api/v1/beneficiaries");
  if (r.status === 401) return signOut("Your session has ended. Sign in again.");
  if (!r.ok) throw new Error(errorText(r));
  renderSlots(r.data.beneficiaries, freshId);
}

async function openHome(message) {
  $("home-mobile").textContent = masked(state.mobile);
  show("home");
  try {
    await refresh();
  } catch (e) {
    setError("person-error", "Couldn't load the people on this number. Reload the page to try again.");
  }
  if (message) toast(message);
}

// Any edit to the form means a new request, so it gets a new idempotency key.
$("form-person").addEventListener("input", () => { state.pendingKey = null; });

$("form-person").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  const body = {
    full_name: f.full_name.value.trim(),
    birth_year: Number(f.birth_year.value),
    gender: (f.querySelector("input[name=gender]:checked") || {}).value,
    photo_id_type: f.photo_id_type.value,
    photo_id_number: f.photo_id_number.value.trim(),
  };
  const year = new Date().getFullYear();
  const problems = [];
  if (!body.full_name) problems.push("Enter the full name");
  if (!(body.birth_year >= 1900 && body.birth_year <= year)) problems.push(`Enter a year of birth between 1900 and ${year}`);
  if (!body.gender) problems.push("Choose a gender");
  if (!body.photo_id_type) problems.push("Choose a photo ID");
  if (body.photo_id_number.replace(/[\s\-/.]/g, "").length < 4) problems.push("Enter the ID number (at least 4 characters)");
  if (problems.length) {
    setError("person-error", problems.join(". ") + ".");
    return;
  }

  // Keep the same key until the server gives a definite answer, so pressing
  // the button again after a dropped connection is a safe retry.
  state.pendingKey = state.pendingKey || newKey();
  setError("person-error", "");
  busy(f, true);
  $("add-person").textContent = "Adding…";
  try {
    const r = await api("/api/v1/beneficiaries", {
      method: "POST", body, headers: { "Idempotency-Key": state.pendingKey },
    });
    if (r.status >= 500) {
      setError("person-error", "The server couldn't save this right now. Press Add person again; you won't be added twice.");
      return;
    }
    state.pendingKey = null;
    if (r.status === 401) return signOut("Your session has ended. Sign in again.");
    if (!r.ok) {
      setError("person-error", errorText(r));
      return;
    }
    const replay = r.headers.get("Idempotent-Replayed") === "true";
    f.reset();
    await refresh(r.data.beneficiary_id);
    toast(replay ? `${body.full_name} was already added. Your retry didn't create a duplicate.`
      : `${body.full_name} added.`);
  } catch (err) {
    if (err instanceof NetworkError) {
      setError("person-error", "Couldn't reach the server. Press Add person again; you won't be added twice.");
    } else {
      setError("person-error", "Something went wrong. Try again.");
    }
  } finally {
    busy(f, false);
    $("add-person").textContent = "Add person";
  }
});

function signOut(message) {
  state.token = null;
  state.pendingKey = null;
  store.clear();
  $("mobile").value = state.mobile || "";
  show("mobile");
  if (message) setError("mobile-error", message, null);
}

$("sign-out").addEventListener("click", () => signOut());

// ---------- start -------------------------------------------------------------------
(function start() {
  const saved = store.load();
  if (saved && saved.token) {
    state.mobile = saved.mobile;
    state.token = saved.token;
    openHome();
  } else {
    show("mobile");
  }
})();
