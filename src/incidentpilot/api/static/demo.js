"use strict";
const $ = id => document.getElementById(id);
let activeRun = "", artifact = null, timer = null, runtime = {}, loading = false;
const text = (id, value) => { $(id).textContent = value ?? ""; };
const format = value => JSON.stringify(value, null, 2);
function message(value, error = false) { text("message", value); $("message").classList.toggle("error", error); }
async function api(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Authorization": `Bearer ${$("token").value}`, "Content-Type": "application/json" }, cache: "no-store" });
  if (!response.ok) { const data = await response.json().catch(() => ({})); throw new Error(`${response.status} · ${typeof data.detail === "string" ? data.detail : "请求失败，请检查凭据、权限或 Run ID"}`); }
  return response.json();
}
function detail(parent, title, value) {
  const item = document.createElement("details"), summary = document.createElement("summary"), pre = document.createElement("pre");
  summary.textContent = title; pre.textContent = typeof value === "string" ? value : format(value); pre.tabIndex = 0;
  item.append(summary, pre); parent.append(item);
}
function setApprovalEnabled(enabled) { for (const id of ["approve", "reject"]) $(id).disabled = !enabled; }
async function load() {
  if (loading) return;
  clearTimeout(timer); loading = true; $("load").disabled = true; $("workspace").setAttribute("aria-busy", "true");
  try {
    const requestedRun = $("run-id").value.trim();
    if (requestedRun !== activeRun) { $("workspace").hidden = true; artifact = null; setApprovalEnabled(false); }
    activeRun = requestedRun; const base = `/v1/runs/${encodeURIComponent(activeRun)}`;
    const [run, ev, calls, patches, tests, approval, mode] = await Promise.all([api(base), api(base + "/evidence"), api(base + "/tool-calls"), api(base + "/patch-artifact"), api(base + "/tests"), api(base + "/approval"), api("/v1/runtime")]);
    runtime = mode;
    const incident = await api(`/v1/incidents/${encodeURIComponent(run.incident_id)}`);
    render(run, incident, ev, calls, patches, tests, approval); $("workspace").hidden = false;
    message(`已更新 ${new Date().toLocaleTimeString("zh-CN")} · ${activeRun}`);
    if (["PENDING", "RUNNING"].includes(run.status)) timer = setTimeout(load, 2500);
  } catch (error) { artifact = null; setApprovalEnabled(false); message(error.message, true); }
  finally { loading = false; $("load").disabled = false; $("workspace").setAttribute("aria-busy", "false"); }
}
function render(run, incident, evidence, calls, patches, tests, approval) {
  text("incident-title", incident.title); text("incident-detail", `${incident.service} · ${incident.environment} · ${incident.severity}`); text("symptom", incident.symptom);
  text("run-status", run.status); text("workflow", run.workflow_state); text("run-error", run.error);
  $("modes").replaceChildren();
  const model = run.result.model?.provider === "fake" ? "Fake 模型" : run.result.model ? `真实模型 · ${run.result.model.model}` : `模型尚未运行 · 当前配置 ${runtime.model}`;
  for (const label of [model, runtime.github === "mock" ? "Mock GitHub · 无外部写入" : "真实 GitHub 已启用", tests.length ? `执行环境 · ${tests.at(-1).mode}` : `沙箱配置 · ${runtime.sandbox}`]) { const span = document.createElement("span"); span.className = "mode"; span.textContent = label; $("modes").append(span); }
  $("plan").replaceChildren(); for (const step of run.plan) { const li = document.createElement("li"); li.textContent = step; $("plan").append(li); } $("plan-empty").hidden = Boolean(run.plan.length);
  $("hypotheses").replaceChildren(); const hypotheses = run.result.hypotheses || [];
  if (!hypotheses.length) text("hypotheses", "尚无已保存的假设。");
  for (const h of hypotheses) { const item = document.createElement("div"); item.className = "hypothesis"; const title = document.createElement("strong"), note = document.createElement("p"); title.textContent = `${h.status} · ${h.statement}`; note.textContent = h.verification_notes || "等待验证"; item.append(title, note); $("hypotheses").append(item); }
  $("calls").replaceChildren(); for (const call of calls) { const tr = document.createElement("tr"); const name = document.createElement("td"), status = document.createElement("td"), latency = document.createElement("td"), time = document.createElement("small"); name.textContent = call.tool_name; time.textContent = new Date(call.created_at).toLocaleTimeString("zh-CN"); name.append(time); status.textContent = call.status; latency.textContent = call.latency_ms == null ? "—" : `${Math.round(call.latency_ms)} ms`; tr.append(name, status, latency); $("calls").append(tr); } $("calls-empty").hidden = Boolean(calls.length);
  text("root-summary", run.root_cause?.summary || "尚无已验证结论。证据不足时工作流会停止。"); text("root-meta", run.root_cause ? `${run.root_cause.fault_category} · ${run.root_cause.confidence} · ${run.root_cause.verified ? "通过证据及模型验证，因果仍需审阅" : "单次基线推理，未独立验证"}` : ""); text("root-evidence", run.root_cause ? `引用：${(run.root_cause.evidence_ids || []).join(" · ")}` : "");
  $("evidence").replaceChildren(); if (!evidence.length) text("evidence", "尚无已保存的有效证据。"); for (const e of evidence) detail($("evidence"), `${e.evidence_id} · ${e.source} · ${e.summary || "展开原始结果"}`, e.result);
  artifact = patches.find(p => p.patch_artifact_id === run.result.patch_artifact_id) || null;
  text("artifact-meta", artifact ? `${artifact.patch_artifact_id}\nSHA-256 ${artifact.patch_hash}\nBase ${artifact.base_commit_sha} · Test ${artifact.test_run_id}` : "尚无通过测试的补丁产物。"); text("diff", artifact?.patch_diff || "尚未生成补丁。");
  text("test-scope", run.result.test_scope === "fault-active" ? "故障保持激活的独立回归测试" : "健康路径回归：通过不代表原故障已解决。"); $("tests").replaceChildren(); if (!tests.length) text("tests", "尚无测试结果。"); for (const t of tests) detail($("tests"), `${t.status} · ${t.mode} · ${t.test_run_id} · exit ${t.exit_code}`, `${t.stdout}\n${t.stderr}`);
  text("approval-state", approval ? `${approval.decision} · ${approval.actor} · ${new Date(approval.decided_at).toLocaleString("zh-CN")} · ${approval.reason}` : run.status === "WAITING_APPROVAL" ? "等待有审批权限的人员审阅当前产物。" : "尚未进入审批阶段。");
  setApprovalEnabled(run.status === "WAITING_APPROVAL" && artifact && ["admin", "approver"].includes(runtime.role));
  const pr = run.result.pull_request; text("pr", pr ? `${pr.mode === "mock" ? "Mock PR（未创建真实 PR）" : "真实 PR"} · ${pr.url}` : "尚未创建 PR。");
}
$("connect").addEventListener("submit", event => { event.preventDefault(); load(); });
async function decide(decision) {
  if (!artifact) return;
  setApprovalEnabled(false);
  try { await api(`/v1/runs/${encodeURIComponent(activeRun)}/approval`, { method: "POST", body: format({ decision, reason: $("reason").value, patch_artifact_id: artifact.patch_artifact_id, patch_hash: artifact.patch_hash }) }); await load(); }
  catch (error) { message(error.message, true); }
}
$("approve").addEventListener("click", () => decide("APPROVE")); $("reject").addEventListener("click", () => decide("REJECT"));
