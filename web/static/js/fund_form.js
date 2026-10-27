/* Create / edit dialogs for companies and funds (admin only). */
"use strict";

/** Company dialog. company = null for a new company. */
App.companyDialog = function (company, onSaved) {
  const isNew = !company;
  const d = document.createElement("dialog");
  d.innerHTML =
    '<form method="dialog" id="co-form"><h2>' + (isNew ? "New company" : "Company settings") + '</h2><div id="cmsg" hidden></div>' +
    '<div class="form-grid" style="grid-template-columns:1fr 2fr">' +
    '<div class="field"><label>Company code</label><input type="text" id="c-code" data-field="code" maxlength="40"' + (isNew ? "" : " disabled") + '>' +
    '<div class="hint">' + (isNew ? "Short, e.g. ABC. Cannot be changed later." : "Cannot be changed") + '</div><div class="err" data-err="code"></div></div>' +
    '<div class="field"><label>Company name</label><input type="text" id="c-name" data-field="name" maxlength="200"><div class="err" data-err="name"></div></div>' +
    (isNew ? "" : '<label class="check" style="grid-column:1/3"><input type="checkbox" id="c-arch"> Archived (hidden from the companies list)</label>') +
    '</div><div class="actions" style="margin-top:16px;justify-content:flex-end"><button type="button" id="c-cancel">Cancel</button><button class="primary" type="submit" id="c-save">Save</button></div></form>';
  document.body.appendChild(d);
  const c = company || { code: "", name: "", archived: false, extra: {} };
  App.$("#c-code", d).value = c.code;
  App.$("#c-name", d).value = c.name;
  if (!isNew) App.$("#c-arch", d).checked = !!c.archived;
  App.$("#c-cancel", d).onclick = () => { d.close(); d.remove(); };
  App.$("#co-form", d).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const body = { code: App.$("#c-code", d).value.trim(), name: App.$("#c-name", d).value.trim(),
      archived: isNew ? false : App.$("#c-arch", d).checked, extra: c.extra || {} };
    App.$("#c-save", d).disabled = true;
    try {
      const saved = isNew ? await App.api("POST", "/api/companies", body)
        : await App.api("PUT", "/api/companies/" + encodeURIComponent(c.code), body);
      d.close(); d.remove(); onSaved(saved);
    } catch (e) { App.showErrors(App.$("#cmsg", d), e, d); }
    finally { App.$("#c-save", d).disabled = false; }
  });
  d.showModal();
};

/** Fund dialog. fund = null for a new fund; presetCompany = company code to pre-select for a new fund. */
App.fundDialog = async function (fund, onSaved, presetCompany) {
  const isNew = !fund;
  const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  let companies = [];
  try { companies = await App.api("GET", "/api/companies"); } catch (e) { /* shown below as an empty list */ }
  const d = document.createElement("dialog");
  d.innerHTML =
    '<form method="dialog" id="fund-form"><h2>' + (isNew ? "New fund" : "Fund settings") + '</h2><div id="fmsg" hidden></div>' +
    '<div class="form-grid" style="grid-template-columns:1fr 1fr">' +
    '<div class="field" style="grid-column:1/3"><label>Company</label><select id="f-co" data-field="company">' +
    '<option value="">— choose the company —</option>' +
    companies.map((c) => '<option value="' + App.esc(c.code) + '">' + App.esc(c.name) + " (" + App.esc(c.code) + ")</option>").join("") +
    '</select><div class="err" data-err="company"></div></div>' +
    '<div class="field"><label>Fund code</label><input type="text" data-field="code" id="f-code" maxlength="40"' + (isNew ? "" : " disabled") + '>' +
    '<div class="hint">' + (isNew ? "Short and unique, e.g. ABC-PF. Cannot be changed later." : "Cannot be changed") + '</div><div class="err" data-err="code"></div></div>' +
    '<div class="field"><label>TDS rate on coupons (%)</label><input type="text" class="num" data-field="tds_rate" id="f-tds"><div class="err" data-err="tds_rate"></div></div>' +
    '<div class="field" style="grid-column:1/3"><label>Fund name</label><input type="text" data-field="name" id="f-name" maxlength="200" placeholder="e.g. Employees\' Provident Fund"><div class="err" data-err="name"></div></div>' +
    '<div class="field"><label>Financial year ends: month</label><select id="f-m" data-field="year_end_month">' +
    months.map((m, i) => '<option value="' + (i + 1) + '">' + m + "</option>").join("") + "</select></div>" +
    '<div class="field"><label>Day</label><input type="text" class="num" id="f-d" data-field="year_end_day"><div class="err" data-err="year_end_day"></div></div>' +
    (isNew ? "" : '<label class="check" style="grid-column:1/3"><input type="checkbox" id="f-arch"> Archived (hidden from the funds list)</label>') +
    '</div><div class="actions" style="margin-top:16px;justify-content:flex-end"><button type="button" id="f-cancel">Cancel</button><button class="primary" type="submit" id="f-save">Save</button></div></form>';
  document.body.appendChild(d);
  const f = fund || { code: "", company: presetCompany || "", name: "", tds_rate: 0, year_end_month: 6, year_end_day: 30, archived: false, extra: {} };
  App.$("#f-co", d).value = f.company || "";
  App.$("#f-code", d).value = f.code;
  App.$("#f-name", d).value = f.name;
  App.$("#f-tds", d).value = String(+(f.tds_rate * 100).toFixed(6));
  App.$("#f-m", d).value = String(f.year_end_month);
  App.$("#f-d", d).value = String(f.year_end_day);
  if (!isNew) App.$("#f-arch", d).checked = !!f.archived;
  App.$("#f-cancel", d).onclick = () => { d.close(); d.remove(); };
  App.$("#fund-form", d).addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const tds = App.parseNumber(App.$("#f-tds", d).value);
    const day = App.parseNumber(App.$("#f-d", d).value);
    if (!App.$("#f-co", d).value) { App.message(App.$("#fmsg", d), "bad", "Choose the company this fund belongs to."); return; }
    if (tds === null || Number.isNaN(tds) || tds < 0 || tds >= 100) { App.message(App.$("#fmsg", d), "bad", "TDS rate must be a percentage from 0 to below 100."); return; }
    if (!Number.isInteger(day)) { App.message(App.$("#fmsg", d), "bad", "Year-end day must be a whole number."); return; }
    const body = { code: App.$("#f-code", d).value.trim(), company: App.$("#f-co", d).value, name: App.$("#f-name", d).value.trim(),
      tds_rate: Math.round(tds * 1e6) / 1e8, year_end_month: Number(App.$("#f-m", d).value), year_end_day: day,
      archived: isNew ? false : App.$("#f-arch", d).checked, extra: f.extra || {} };
    App.$("#f-save", d).disabled = true;
    try {
      const saved = isNew ? await App.api("POST", "/api/funds", body)
        : await App.api("PUT", "/api/funds/" + encodeURIComponent(f.code), body);
      d.close(); d.remove(); onSaved(saved);
    } catch (e) { App.showErrors(App.$("#fmsg", d), e, d); }
    finally { App.$("#f-save", d).disabled = false; }
  });
  d.showModal();
};
