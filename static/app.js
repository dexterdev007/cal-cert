const state = {
  token: localStorage.getItem("calcertToken") || "",
  role: localStorage.getItem("calcertRole") || "",
};

const app = document.querySelector("#app");
const sessionBar = document.querySelector("#sessionBar");

function todayIso() {
  return new Date().toISOString().slice(0, 10);
}

function addMonths(dateText, months) {
  const current = new Date(`${dateText}T00:00:00`);
  current.setMonth(current.getMonth() + months);
  return current.toISOString().slice(0, 10);
}

function htmlEscape(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) {
    headers.Authorization = `Bearer ${state.token}`;
  }
  const response = await fetch(path, { ...options, headers });
  const contentType = response.headers.get("content-type") || "";
  const payload = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const message = payload && payload.error ? payload.error : "Request failed";
    throw new Error(message);
  }
  return payload;
}

function setSessionBar() {
  sessionBar.innerHTML = "";
  if (!state.token) {
    return;
  }
  const role = document.createElement("span");
  role.className = "status-pill";
  role.textContent = state.role === "admin" ? "Admin" : "User";
  const logout = document.createElement("button");
  logout.className = "ghost-button";
  logout.type = "button";
  logout.textContent = "Sign out";
  logout.addEventListener("click", () => {
    localStorage.removeItem("calcertToken");
    localStorage.removeItem("calcertRole");
    state.token = "";
    state.role = "";
    render();
  });
  sessionBar.append(role, logout);
}

function render() {
  setSessionBar();
  if (!state.token) {
    renderLogin();
  } else if (state.role === "admin") {
    renderAdmin();
  } else {
    renderUser();
  }
}

function renderLogin() {
  app.innerHTML = document.querySelector("#loginTemplate").innerHTML;
  const role = document.querySelector("#loginRole");
  const email = document.querySelector("#loginEmail");
  const password = document.querySelector("#loginPassword");
  const error = document.querySelector("#loginError");

  function fill() {
    if (role.value === "admin") {
      email.value = "admin@calcert.local";
      password.value = "admin123";
    } else {
      email.value = "engineer@calcert.local";
      password.value = "user123";
    }
  }

  role.addEventListener("change", fill);
  fill();

  document.querySelector("#loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    error.textContent = "";
    try {
      const payload = await api("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: email.value, password: password.value }),
      });
      state.token = payload.token;
      state.role = payload.role;
      localStorage.setItem("calcertToken", state.token);
      localStorage.setItem("calcertRole", state.role);
      render();
    } catch (err) {
      error.textContent = err.message;
    }
  });
}

async function renderAdmin() {
  app.innerHTML = document.querySelector("#adminTemplate").innerHTML;
  document.querySelector("#uploadButton").addEventListener("click", uploadPdf);
  document.querySelector("#sampleButton").addEventListener("click", loadSample);
  document.querySelector("#refreshCertificates").addEventListener("click", loadCertificates);
  await loadCertificates();
}

async function uploadPdf() {
  const input = document.querySelector("#pdfFile");
  const error = document.querySelector("#adminError");
  const success = document.querySelector("#adminSuccess");
  error.textContent = "";
  success.textContent = "";
  if (!input.files.length) {
    error.textContent = "Choose a PDF first.";
    return;
  }
  const formData = new FormData();
  formData.append("file", input.files[0]);
  try {
    const payload = await api("/api/admin/upload", {
      method: "POST",
      body: formData,
    });
    success.textContent = `Extracted ${payload.upload.extracted_count} certificate records.`;
    renderCertificateRows(payload.certificates);
    document.querySelector("#lastUploadCount").textContent = payload.upload.extracted_count;
  } catch (err) {
    error.textContent = err.message;
  }
}

async function loadSample() {
  const error = document.querySelector("#adminError");
  const success = document.querySelector("#adminSuccess");
  error.textContent = "";
  success.textContent = "";
  try {
    const payload = await api("/api/admin/load-sample", { method: "POST" });
    success.textContent = payload.upload.duplicate
      ? `Sample already loaded with ${payload.upload.extracted_count} records.`
      : `Sample loaded with ${payload.upload.extracted_count} records.`;
    renderCertificateRows(payload.certificates);
    document.querySelector("#lastUploadCount").textContent = payload.upload.extracted_count;
  } catch (err) {
    error.textContent = err.message;
  }
}

async function loadCertificates() {
  try {
    const payload = await api("/api/admin/certificates");
    renderCertificateRows(payload.certificates);
  } catch (err) {
    const error = document.querySelector("#adminError");
    if (error) {
      error.textContent = err.message;
    }
  }
}

function renderCertificateRows(rows) {
  const tbody = document.querySelector("#certificateRows");
  document.querySelector("#recordCount").textContent = rows.length;
  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No historical records loaded.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows
    .map((row) => {
      const pages = row.page_start === row.page_end ? row.page_start : `${row.page_start}-${row.page_end}`;
      return `
        <tr>
          <td>${htmlEscape(row.ulr)}</td>
          <td>${htmlEscape(row.instrument_name)}</td>
          <td>${htmlEscape(row.discipline_parameter)}</td>
          <td>${htmlEscape(row.range_text)}</td>
          <td>${htmlEscape(row.least_count_text)}</td>
          <td>${htmlEscape(pages)}</td>
          <td>${htmlEscape(row.quality_status)}</td>
        </tr>
      `;
    })
    .join("");
}

async function renderUser() {
  app.innerHTML = document.querySelector("#userTemplate").innerHTML;
  state.selectedInstrument = null;
  state.configurations = [];
  state.selectedConfiguration = null;
  state.activeDraft = null;
  const calibrationDate = document.querySelector('[name="calibration_date"]');
  const dueDate = document.querySelector('[name="next_calibration_date"]');
  const issueDate = document.querySelector('[name="issue_date"]');
  const jobNumber = document.querySelector('[name="job_number"]');
  calibrationDate.value = todayIso();
  dueDate.value = addMonths(todayIso(), 12);
  issueDate.value = todayIso();
  jobNumber.value = `JOB-${todayIso().replaceAll("-", "")}-001`;
  calibrationDate.addEventListener("change", () => {
    if (!dueDate.value) {
      dueDate.value = addMonths(calibrationDate.value, 12);
    }
    if (!issueDate.value) {
      issueDate.value = calibrationDate.value;
    }
  });
  setupInstrumentSearch();
  document.querySelector("#configurationSelect").addEventListener("change", selectConfiguration);
  document.querySelector("#jobForm").addEventListener("submit", createReviewDraft);
  document.querySelector("#refreshGenerated").addEventListener("click", loadGeneratedHistory);
  await loadGeneratedHistory();
}

function setupInstrumentSearch() {
  const input = document.querySelector("#instrumentSearch");
  const suggestions = document.querySelector("#instrumentSuggestions");
  let timer;
  input.addEventListener("input", () => {
    state.selectedInstrument = null;
    state.selectedConfiguration = null;
    resetConfigurationUi();
    clearTimeout(timer);
    const query = input.value.trim();
    if (!query) {
      suggestions.hidden = true;
      input.setAttribute("aria-expanded", "false");
      return;
    }
    timer = setTimeout(() => searchInstruments(query), 120);
  });
  input.addEventListener("keydown", (event) => {
    const options = [...suggestions.querySelectorAll("button")];
    const current = options.indexOf(document.activeElement);
    if (event.key === "ArrowDown" && options.length) {
      event.preventDefault();
      options[Math.min(current + 1, options.length - 1)].focus();
    } else if (event.key === "Escape") {
      suggestions.hidden = true;
      input.setAttribute("aria-expanded", "false");
    }
  });
}

async function searchInstruments(query) {
  const input = document.querySelector("#instrumentSearch");
  const suggestions = document.querySelector("#instrumentSuggestions");
  try {
    const result = await api(`/api/instruments?q=${encodeURIComponent(query)}`);
    if (input.value.trim() !== query) return;
    if (!result.instruments.length) {
      suggestions.innerHTML = `<li class="suggestion-empty">No historical instrument found</li>`;
    } else {
      suggestions.innerHTML = result.instruments
        .map(
          (instrument) => `
            <li role="option">
              <button type="button" data-id="${instrument.id}" data-name="${htmlEscape(instrument.name)}">
                <span>${htmlEscape(instrument.name)}</span>
                <small>${instrument.usage_count} historical record${instrument.usage_count === 1 ? "" : "s"}</small>
              </button>
            </li>`
        )
        .join("");
      suggestions.querySelectorAll("button").forEach((button) => {
        button.addEventListener("click", () => chooseInstrument(Number(button.dataset.id), button.dataset.name));
      });
    }
    suggestions.hidden = false;
    input.setAttribute("aria-expanded", "true");
  } catch (err) {
    suggestions.innerHTML = `<li class="suggestion-empty">${htmlEscape(err.message)}</li>`;
    suggestions.hidden = false;
  }
}

async function chooseInstrument(id, name) {
  state.selectedInstrument = { id, name };
  const input = document.querySelector("#instrumentSearch");
  input.value = name;
  input.setAttribute("aria-expanded", "false");
  document.querySelector("#instrumentSuggestions").hidden = true;
  document.querySelector("#instrumentStatus").textContent = "Loading configurations";
  try {
    const result = await api(`/api/instruments/${id}/configurations`);
    state.configurations = result.configurations;
    const select = document.querySelector("#configurationSelect");
    select.disabled = false;
    select.innerHTML = `<option value="">Choose a validated configuration</option>${result.configurations
      .map(
        (config) => `<option value="${config.id}">${htmlEscape(
          [config.manufacturer, config.model, config.range_text, config.least_count_text].filter(Boolean).join(" · ")
        )}</option>`
      )
      .join("")}`;
    document.querySelector("#instrumentStatus").textContent = `${result.configurations.length} valid configuration${result.configurations.length === 1 ? "" : "s"}`;
  } catch (err) {
    document.querySelector("#instrumentStatus").textContent = "Could not load";
    document.querySelector("#configurationFacts").textContent = err.message;
  }
}

function resetConfigurationUi() {
  const select = document.querySelector("#configurationSelect");
  select.disabled = true;
  select.innerHTML = `<option value="">Select an instrument first</option>`;
  document.querySelector("#instrumentStatus").textContent = "Not selected";
  document.querySelector("#configurationFacts").className = "configuration-facts empty-state";
  document.querySelector("#configurationFacts").textContent = "Select an instrument and one of its historical configurations.";
  document.querySelector("#measurementSection").hidden = true;
  document.querySelector("#environmentSection").hidden = true;
  document.querySelector("#draftActions").hidden = true;
}

function selectConfiguration(event) {
  const config = state.configurations.find((item) => item.id === Number(event.target.value));
  state.selectedConfiguration = config || null;
  if (!config) {
    document.querySelector("#measurementSection").hidden = true;
    document.querySelector("#environmentSection").hidden = true;
    document.querySelector("#draftActions").hidden = true;
    return;
  }
  renderConfigurationFacts(config);
  renderMeasurementForm(config.measurement_schema);
}

function renderConfigurationFacts(config) {
  const facts = document.querySelector("#configurationFacts");
  facts.className = "configuration-facts";
  facts.innerHTML = `
    <div><span>Manufacturer</span><strong>${htmlEscape(config.manufacturer || "Not recorded")}</strong></div>
    <div><span>Model</span><strong>${htmlEscape(config.model || "Not recorded")}</strong></div>
    <div><span>Range</span><strong>${htmlEscape(config.range_text)}</strong></div>
    <div><span>Least count</span><strong>${htmlEscape(config.least_count_text)}</strong></div>
    <div><span>Procedure</span><strong>${htmlEscape(config.calibration_procedure)}</strong></div>
    <div><span>Uncertainty model</span><strong>${htmlEscape(config.uncertainty_model.name)} v${htmlEscape(config.uncertainty_model.version)}</strong></div>
  `;
}

function renderMeasurementForm(sections) {
  const container = document.querySelector("#dynamicMeasurements");
  let requiredCount = 0;
  const renderedSections = sections
    .map((section) => {
      const activeColumns = section.columns.filter((column) => column.kind !== "calculated");
      const measurementColumns = activeColumns.filter((column) => column.kind === "measurement");
      if (!measurementColumns.length) return "";
      const rows = section.rows
        .filter((row) => !row.is_summary)
        .map((row) => {
          const cells = activeColumns.map((column) => {
            if (column.kind === "historical") {
              return `<td class="historical-cell">${htmlEscape(row.static_values[column.key] || "—")}</td>`;
            }
            const fieldId = `s${section.index}.r${row.index}.${column.key}`;
            requiredCount += 1;
            return `<td class="current-cell"><input data-measurement="${fieldId}" aria-label="${htmlEscape(column.label)} row ${row.index + 1}" required placeholder="Reading or repeats: 1.0, 1.1"></td>`;
          });
          return `<tr>${cells.join("")}</tr>`;
        })
        .join("");
      return `
        <section class="measurement-block">
          <h3>${htmlEscape(section.name)}</h3>
          <div class="table-wrap"><table class="measurement-table">
            <thead><tr>${activeColumns.map((column) => `<th class="${column.kind}-heading">${htmlEscape(column.label)}</th>`).join("")}</tr></thead>
            <tbody>${rows}</tbody>
          </table></div>
        </section>`;
    })
    .join("");
  container.innerHTML = renderedSections || `<div class="blocking-message">This historical configuration has no reliable measurement schema and cannot create an automated draft.</div>`;
  document.querySelector("#measurementCount").textContent = `${requiredCount} required reading${requiredCount === 1 ? "" : "s"}`;
  document.querySelector("#measurementSection").hidden = false;
  document.querySelector("#environmentSection").hidden = requiredCount === 0;
  document.querySelector("#draftActions").hidden = requiredCount === 0;
  setWorkflowStep("measurements");
}

function formPayload(form) {
  const data = new FormData(form);
  const payload = {};
  for (const [key, value] of data.entries()) payload[key] = value;
  payload.configuration_id = Number(payload.configuration_id);
  payload.environment = {
    temperature: payload.temperature,
    humidity: payload.humidity,
    pressure: payload.pressure,
  };
  delete payload.temperature;
  delete payload.humidity;
  delete payload.pressure;
  payload.measurements = {};
  form.querySelectorAll("[data-measurement]").forEach((input) => {
    payload.measurements[input.dataset.measurement] = input.value;
  });
  return payload;
}

async function createReviewDraft(event) {
  event.preventDefault();
  const error = document.querySelector("#userError");
  error.textContent = "";
  if (!state.selectedInstrument || !state.selectedConfiguration) {
    error.textContent = "Select an instrument and a valid historical configuration.";
    return;
  }
  try {
    const result = await api("/api/jobs/draft", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(formPayload(event.currentTarget)),
    });
    state.activeDraft = result;
    renderDraftReview();
  } catch (err) {
    error.textContent = err.message;
  }
}

function scoreClass(score) {
  return score < 0.6 ? "score low" : "score";
}

function setWorkflowStep(step) {
  const order = ["instrument", "measurements", "review"];
  const active = order.indexOf(step);
  document.querySelectorAll(".workflow-steps li").forEach((item, index) => {
    item.classList.toggle("active", index === active);
    item.classList.toggle("complete", index < active);
  });
}

function renderDraftReview() {
  const result = state.activeDraft;
  const draft = result.draft;
  const panel = document.querySelector("#resultPanel");
  const review = document.querySelector("#reviewWorkspace");
  review.hidden = false;
  setWorkflowStep("review");
  panel.innerHTML = `
    <div class="review-summary">
      <div><span>Job</span><strong>${htmlEscape(draft.job.job_number)}</strong></div>
      <div><span>Instrument</span><strong>${htmlEscape(draft.instrument.name)}</strong></div>
      <div><span>Source ULR</span><strong>${htmlEscape(draft.historical.source_ulr)}</strong></div>
      <div><span>Confidence</span><strong class="${scoreClass(draft.historical.confidence_score)}">${Math.round(draft.historical.confidence_score * 100)}%</strong></div>
    </div>
    <div class="uncertainty-notice">
      <strong>${htmlEscape(draft.uncertainty.statement)}</strong>
      <span>${htmlEscape(draft.uncertainty.model)} v${htmlEscape(draft.uncertainty.version)} · ${htmlEscape(draft.uncertainty.validation_status.replaceAll("_", " "))}</span>
    </div>
    ${draft.result_sections.map(renderReviewSection).join("")}
    <div class="candidate-review">
      <h3>Historical candidates</h3>
      ${result.candidates
        .map(
          (candidate) => `<label class="candidate-option">
            <input type="radio" name="reviewCandidate" value="${candidate.certificate_id}" ${candidate.certificate_id === draft.historical.source_certificate_id ? "checked" : ""}>
            <span class="${scoreClass(candidate.score)}">${Math.round(candidate.score * 100)}%</span>
            <span><strong>${htmlEscape(candidate.ulr)}</strong><small>${htmlEscape(candidate.calibration_date || "Date unavailable")} · ${htmlEscape(candidate.tier)}</small></span>
          </label>`
        )
        .join("")}
      <button id="switchCandidate" class="secondary-button" type="button">Use selected candidate</button>
    </div>
    <div class="review-actions">
      <button id="editDraft" class="secondary-button" type="button">Edit measurements</button>
      <button id="rejectDraft" class="danger-button" type="button">Reject draft</button>
      <button id="approveDraft" class="primary-button" type="button">Approve and generate PDF</button>
    </div>
    <p id="reviewMessage" class="form-error"></p>
  `;
  document.querySelector("#switchCandidate").addEventListener("click", switchReviewCandidate);
  document.querySelector("#editDraft").addEventListener("click", () => {
    document.querySelector("#measurementSection").scrollIntoView({ behavior: "smooth" });
  });
  document.querySelector("#rejectDraft").addEventListener("click", rejectDraft);
  document.querySelector("#approveDraft").addEventListener("click", approveDraft);
  review.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderReviewSection(section) {
  return `
    <section class="review-table-block">
      <h3>${htmlEscape(section.name)}</h3>
      <div class="table-wrap"><table>
        <thead><tr>${section.headers.map((header) => `<th>${htmlEscape(header)}</th>`).join("")}</tr></thead>
        <tbody>${section.rows
          .map(
            (row) => `<tr>${row.values
              .map((value, index) => `<td class="provenance-${row.provenance[index]}">${htmlEscape(value || "—")}</td>`)
              .join("")}</tr>`
          )
          .join("")}</tbody>
      </table></div>
    </section>`;
}

async function switchReviewCandidate() {
  const selected = document.querySelector('[name="reviewCandidate"]:checked');
  const message = document.querySelector("#reviewMessage");
  if (!selected) return;
  try {
    const updated = await api(`/api/jobs/${state.activeDraft.job_id}/candidate`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ certificate_id: Number(selected.value) }),
    });
    state.activeDraft.draft = updated.draft;
    renderDraftReview();
  } catch (err) {
    message.textContent = err.message;
  }
}

async function rejectDraft() {
  const message = document.querySelector("#reviewMessage");
  try {
    await api(`/api/jobs/${state.activeDraft.job_id}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: "Rejected during engineer review" }),
    });
    document.querySelector("#resultPanel").innerHTML = `<div class="decision-message rejected"><strong>Draft rejected</strong><span>No certificate was issued.</span></div>`;
  } catch (err) {
    message.textContent = err.message;
  }
}

async function approveDraft() {
  const button = document.querySelector("#approveDraft");
  const message = document.querySelector("#reviewMessage");
  button.disabled = true;
  try {
    const approved = await api(`/api/jobs/${state.activeDraft.job_id}/approve`, { method: "POST" });
    document.querySelector("#resultPanel").innerHTML = `
      <div class="decision-message approved">
        <div><strong>${htmlEscape(approved.certificate_number)}</strong><span>Approved and finalized</span></div>
        <a class="primary-button" href="${approved.pdf_url}?token=${encodeURIComponent(state.token)}" target="_blank" rel="noreferrer">Open PDF</a>
      </div>`;
    await loadGeneratedHistory();
  } catch (err) {
    button.disabled = false;
    message.textContent = err.message;
  }
}

async function loadGeneratedHistory() {
  try {
    const payload = await api("/api/generated");
    const tbody = document.querySelector("#generatedRows");
    if (!payload.generated.length) {
      tbody.innerHTML = `<tr><td colspan="5" class="muted">No generated certificates yet.</td></tr>`;
      return;
    }
    tbody.innerHTML = payload.generated
      .map(
        (row) => `
          <tr>
            <td><a href="/api/generated/${row.id}/pdf?token=${encodeURIComponent(state.token)}" target="_blank" rel="noreferrer">${htmlEscape(row.certificate_number)}</a></td>
            <td>${htmlEscape(row.job_number)}</td>
            <td>${htmlEscape(row.client_name)}</td>
            <td>${htmlEscape(row.instrument_name)}</td>
            <td><span class="${scoreClass(row.confidence_score)}">${Math.round(row.confidence_score * 100)}%</span></td>
          </tr>
        `
      )
      .join("");
  } catch (err) {
    const error = document.querySelector("#userError");
    if (error) {
      error.textContent = err.message;
    }
  }
}

render();
