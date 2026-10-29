"use strict";
const $ = (s) => document.querySelector(s);
const $$ = (s) => [...document.querySelectorAll(s)];
const state = {
  health: null,
  session: null,
  task: null,
  tasks: [],
  file: null,
  filter: "all",
  search: "",
  speaker: "",
  poll: null,
  previewPoll: null,
  edit: null,
  role: null,
  toastTimer: null,
  requestGeneration: 0,
  previewTask: null,
  playbackEnd: null,
};
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const clock = (seconds, precise = false) => {
  const ms = Math.round(Number(seconds || 0) * 1000),
    h = Math.floor(ms / 3600000),
    m = Math.floor(ms / 60000) % 60,
    s = Math.floor(ms / 1000) % 60;
  return `${h.toString().padStart(2, "0")}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}${precise ? "." + (ms % 1000).toString().padStart(3, "0") : ""}`;
};
const statusName = {
  queued: "等待处理",
  running: "正在转写",
  completed: "等待核听",
  failed: "处理失败",
  cancelled: "已停止",
  interrupted: "处理中断",
};
async function api(url, options = {}) {
  const res = await fetch(url, {
    ...options,
    headers: {
      ...(state.session?.csrf ? { "X-CSRF-Token": state.session.csrf } : {}),
      ...(options.body && !(options.body instanceof FormData)
        ? { "Content-Type": "application/json" }
        : {}),
      ...options.headers,
    },
  });
  if (!res.ok) {
    if (res.status === 401 && state.session?.required && !url.endsWith('/auth/login')) {
      stopPolling(); clearMedia();
      location.replace(location.pathname);
    }
    let message = `请求失败 (${res.status})`;
    try {
      const d = await res.json();
      message = typeof d.detail === "string" ? d.detail : message;
    } catch {}
    throw Error(message);
  }
  return res.json();
}
function toast(message, error = false) {
  $("#toast").textContent = message;
  $("#toast").classList.toggle("error", error);
  $("#toast").hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(
    () => ($("#toast").hidden = true),
    error ? 7000 : 3500,
  );
}
function closeSidebar() {
  $("#sidebar").classList.remove("open");
  $("#mobile-menu").setAttribute("aria-expanded", "false");
}
function openDialog(id) {
  $(id).showModal();
}
function closeDialog(dialog) {
  if (
    dialog.id === "upload-dialog" &&
    $("#start-transcription").disabled &&
    state.uploading
  ) {
    toast("正在上传，请等待上传结束");
    return;
  }
  dialog.close();
}
function speakerLabel(id) {
  const p = state.task?.speakers.find((p) => p.id === id);
  return p?.name || id;
}
function roleLabel(p) {
  return p
    ? `${p.role}${p.role_status === "suggested" ? " · 候选" : p.role_status === "confirmed" ? " · 已确认" : ""}`
    : "身份待确认";
}
function sourcePlayer() {
  return !$("#video-player").hidden ? $("#video-player") : $("#audio-player");
}
async function playAt(start, end = null) {
  if (!state.task || state.task.is_demo) {
    toast("合成演示没有真实声轨；请上传视频进行回听");
    return;
  }
  const player = sourcePlayer();
  if (!player.getAttribute("src")) {
    toast("音轨尚未提取完成");
    return;
  }
  state.playbackEnd = end;
  try {
    if (player.readyState < 1)
      await new Promise((resolve, reject) => {
        player.addEventListener("loadedmetadata", resolve, { once: true });
        player.addEventListener("error", reject, { once: true });
        setTimeout(resolve, 5000);
      });
    player.currentTime = start;
    await player.play();
  } catch {
    toast("无法回放这段媒体，请检查音轨或生成通用视频回放", true);
  }
}
async function loadTasks() {
  try {
    state.tasks = await api("/api/tasks");
    $("#task-list").innerHTML = state.tasks.length
      ? state.tasks
          .map(
            (t) =>
              `<button class="task-link ${state.task?.id === t.id ? "current" : ""}" data-task="${t.id}"><strong>${esc(t.title)}</strong><small>${t.is_demo ? "合成演示" : statusName[t.state] || t.state} · ${new Date(t.created_at).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" })}</small></button>`,
          )
          .join("")
      : '<p class="small muted">还没有转写记录<br>从一段庭审视频开始</p>';
  } catch (e) {
    $("#task-list").textContent = e.message;
  }
}
function stopPolling() {
  clearTimeout(state.poll);
  state.poll = null;
}
function clearMedia() {
  clearTimeout(state.previewPoll);
  state.previewTask = null;
  $("#make-preview").disabled = false;
  for (const p of [$("#audio-player"), $("#video-player")]) {
    p.pause();
    p.removeAttribute("src");
    p.load();
    p.hidden = true;
  }
  $("#source-frame").hidden = true;
  state.playbackEnd = null;
}
async function openTask(id) {
  const generation = ++state.requestGeneration;
  stopPolling();
  try {
    const task = await api(`/api/tasks/${id}`);
    if (generation !== state.requestGeneration) return;
    clearMedia();
    state.task = task;
    state.filter = "all";
    state.speaker = "";
    state.search = "";
    $("#search").value = "";
    $("#empty-view").hidden = true;
    $("#task-view").hidden = false;
    closeSidebar();
    renderTask(true);
    await loadTasks();
    if (["queued", "running"].includes(task.state)) pollTask(id);
    history.replaceState(null, "", `#${id}`);
  } catch (e) {
    toast(e.message, true);
  }
}
function pollTask(id) {
  stopPolling();
  state.poll = setTimeout(async () => {
    if (state.task?.id !== id) return;
    try {
      const task = await api(`/api/tasks/${id}`);
      if (state.task?.id !== id) return;
      state.task = task;
      renderTask(task.state === "completed");
      if (["queued", "running"].includes(task.state)) pollTask(id);
      else loadTasks();
    } catch (e) {
      toast(e.message, true);
    }
  }, 1500);
}
function renderTask(resetMedia = false) {
  const t = state.task;
  $("#task-short-id").textContent = t.id.slice(0, 8).toUpperCase();
  $("#task-title").textContent = t.title;
  $("#task-subtitle").textContent =
    `${t.options.engine === "local" ? "本地语音识别" : "腾讯云会议识别"} · ${new Date(t.created_at).toLocaleString("zh-CN")} · 修订 ${t.revision}`;
  $("#demo-notice").hidden = !t.is_demo;
  $("#task-status").hidden = t.state === "completed";
  $("#stage").textContent = t.stage;
  $("#progress-label").textContent = t.progress + "%";
  $("#progress").value = t.progress;
  $("#task-error").textContent = t.error || "";
  $("#cancel-task").hidden = !["queued", "running"].includes(t.state);
  $("#retry-task").hidden = !["failed", "cancelled", "interrupted"].includes(
    t.state,
  );
  $("#stat-duration").textContent = clock(t.media.duration);
  $("#stat-speakers").textContent =
    `${t.speakers.length} / ${t.speakers.filter((s) => s.role_status !== "confirmed").length}`;
  $("#stat-segments").textContent = t.segments.length;
  $("#stat-reviewed").textContent =
    `${t.segments.filter((s) => s.reviewed).length} / ${t.segments.length}`;
  $("#result-state").textContent =
    t.state === "completed" ? "待核听" : statusName[t.state];
  for (const id of ["export-json", "export-md", "export-audit", "add-speaker"])
    $("#" + id).disabled = t.state !== "completed";
  $("#speaker-list").innerHTML =
    t.speakers
      .map(
        (p, i) =>
          `<button class="speaker-card" data-role="${esc(p.id)}"><span class="avatar c${i % 4}">${String(i + 1).padStart(2, "0")}</span><span class="speaker-info"><strong>${esc(p.name || p.id)}</strong><small>${esc(p.role)}</small></span><span class="role-tag ${p.role_status}">${{ unknown: "待确认", suggested: "候选", confirmed: "已确认" }[p.role_status]}</span><span class="small muted">↗</span></button>`,
      )
      .join("") || '<p class="small muted">识别完成后在这里核对人物</p>';
  $("#speaker-filter").innerHTML =
    '<option value="">所有人物</option>' +
    t.speakers
      .map(
        (p) => `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`,
      )
      .join("");
  $("#speaker-filter").value = state.speaker;
  $("#warnings").innerHTML = t.warnings.map((w) => `<p>${esc(w)}</p>`).join("");
  const model = t.manifest.asr_model;
  $("#manifest").innerHTML =
    `<dt>输入 SHA256</dt><dd>${esc(t.manifest.input_sha256 || "待计算")}</dd><dt>识别模型</dt><dd>${esc(model || "尚未记录")}</dd><dt>管线版本</dt><dd>${esc(t.manifest.pipeline_version || "0.1.0")}</dd><dt>角色判定</dt><dd>自述身份线索候选 + 人工核听；无自动视觉身份推断</dd>`;
  $("#make-preview").hidden = !t.media.has_video || t.is_demo;
  $("#show-frame").hidden = !t.media.has_video || t.is_demo;
  $("#media-description").textContent = t.is_demo
    ? "合成演示 · 无真实声轨"
    : "原始音轨提取后可回放";
  $("#preview-hint").textContent = "点击发言时间，即可定位原始音轨";
  if (resetMedia && !t.is_demo && t.state === "completed") {
    $("#audio-player").src = `/api/tasks/${t.id}/media/audio`;
    $("#audio-player").hidden = false;
  }
  renderSegments();
}
function highlight(text) {
  if (!state.search) return esc(text);
  const needle = state.search.toLocaleLowerCase(),
    lower = text.toLocaleLowerCase();
  let at = 0,
    html = "",
    found;
  while ((found = lower.indexOf(needle, at)) !== -1) {
    html +=
      esc(text.slice(at, found)) +
      `<mark>${esc(text.slice(found, found + needle.length))}</mark>`;
    at = found + needle.length;
  }
  return html + esc(text.slice(at));
}
function renderSegments() {
  const t = state.task;
  if (!t) return;
  $$("[data-filter]").forEach((b) => {
    b.classList.toggle("selected", b.dataset.filter === state.filter);
    b.setAttribute("aria-pressed", String(b.dataset.filter === state.filter));
  });
  const segments = [...t.segments]
    .sort(
      (a, b) => a.start - b.start || a.end - b.end || a.id.localeCompare(b.id),
    )
    .filter((s) => {
      if (state.filter === "review" && s.reviewed) return false;
      if (state.filter === "overlap" && !s.overlap_group) return false;
      if (state.speaker && s.speaker_id !== state.speaker) return false;
      const p = t.speakers.find((p) => p.id === s.speaker_id);
      return (
        !state.search ||
        [s.text, p?.name, p?.role, s.speaker_id]
          .join(" ")
          .toLocaleLowerCase()
          .includes(state.search.toLocaleLowerCase())
      );
    });
  $("#segments").innerHTML =
    segments
      .map((s) => {
        const p = t.speakers.find((p) => p.id === s.speaker_id);
        return `<article class="segment ${s.overlap_group ? "overlap" : ""}" data-segment="${s.id}" data-start="${s.start}" data-end="${s.end}"><div class="segment-head"><button class="time-button" data-play="${s.id}" aria-label="回听 ${clock(s.start, true)} 到 ${clock(s.end, true)}">${clock(s.start, true)} <span>— ${clock(s.end, true)}</span></button><span class="segment-speaker">${esc(p?.name || s.speaker_id)}</span><span class="segment-role">${esc(roleLabel(p))}</span><button class="text-button" data-edit="${s.id}">校对 ↗</button></div><p class="segment-text">${highlight(s.text)}</p><div class="segment-notes">${s.reviewed ? '<span class="flag reviewed">✓ 已核听</span>' : '<span class="flag">待核听</span>'}${s.overlap_group ? `<span class="flag">⇉ 同时发言 · ${esc(s.overlap_group)}</span>` : ""}${s.flags
          .filter((f) => !["cloud_asr_review", "overlap_review"].includes(f))
          .map(
            (f) =>
              `<span class="flag">${esc(state.health?.flags[f] || f)}</span>`,
          )
          .join("")}</div></article>`;
      })
      .join("") ||
    `<div class="no-results">${t.state === "completed" ? "没有符合条件的发言片段" : "处理完成后，逐句转写会显示在这里"}</div>`;
  $("#shown-count").textContent =
    `显示 ${segments.length} / ${t.segments.length} 段`;
}
function setFile(file) {
  if (!file) return;
  if (file.size > (state.health?.max_upload_bytes || 4 * 1024 ** 3)) {
    toast("文件超过 4 GB 限制", true);
    return;
  }
  if (!state.health) {
    toast("环境信息尚未读取，请稍后重试", true);
    return;
  }
  state.file = file;
  $("#selected-file-name").textContent = file.name;
  $("#selected-file-size").textContent =
    (file.size / 1024 / 1024).toFixed(1) + " MB";
  $("#upload-error").textContent = "";
  $("#upload-progress").hidden = true;
  $("#diarize").checked = state.health.capabilities.diarization;
  $("#cloud-consent").checked = false;
  updateSetup();
  openDialog("#upload-dialog");
}
function updateSetup() {
  const cloud = $("#engine").value === "tencent",
    cap = state.health?.capabilities;
  $("#cloud-consent-label").hidden = !cloud;
  $("#diarize").disabled = cloud;
  $("#separate").disabled = cloud || !$("#diarize").checked;
  $("#language").disabled = cloud;
  $("#num-speakers").max = cloud ? "10" : "50";
  if (cloud || !$("#diarize").checked) $("#separate").checked = false;
  const needs = [];
  if (!state.health?.ffmpeg) needs.push("FFmpeg");
  if (cloud && !cap?.tencent) needs.push("腾讯云 SDK 与服务端凭证");
  if (!cloud && !cap?.local_asr) needs.push("本地 ASR 依赖");
  if (!cloud && $("#diarize").checked && !cap?.diarization)
    needs.push("说话人分离依赖与权重");
  if ($("#separate").checked && !cap?.overlap_separation)
    needs.push("语音分离依赖与权重");
  $("#setup-status").textContent = state.health?.maintenance
    ? '服务正在维护，已有任务和文件已保留，请稍后刷新。'
    : needs.length
    ? "缺少 " + needs.join("、") + "。请按 README 配置后重启。"
    : cloud
      ? `云端识别将传输本任务音频；原始视频保留在${state.session?.required ? '校园服务器' : '本机'}。`
      : `${state.session?.required ? '服务器' : '本地'}模型：${cap?.model} / ${cap?.device}。权重可用性将在任务加载时检查。${!$("#diarize").checked ? " 当前未区分说话人，也未检测重叠。" : ""}`;
  $("#start-transcription").disabled = Boolean(state.health?.maintenance) || needs.length > 0;
}
function upload() {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    const opts = {
      engine: $("#engine").value,
      language: $("#language").value,
      num_speakers: $("#num-speakers").value
        ? Number($("#num-speakers").value)
        : null,
      audio_stream: Number($("#audio-stream").value),
      diarize: $("#engine").value === "tencent" || $("#diarize").checked,
      separate_overlap: $("#separate").checked,
      cloud_consent: $("#cloud-consent").checked,
    };
    form.append("file", state.file);
    opts.role_agent = $("#role-agent").checked;
    opts.visual_evidence = $("#visual-evidence").checked;
    if (state.session?.mode === 'platform') {
      uploadParts(opts).then(resolve, reject);
      return;
    }
    form.append("options", JSON.stringify(opts));
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/tasks");
    if (state.session?.csrf) xhr.setRequestHeader('X-CSRF-Token', state.session.csrf);
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable)
        $("#upload-progress").value = (e.loaded / e.total) * 100;
    };
    xhr.onerror = () => reject(Error("上传连接失败，文件仍保留在本机，可重试"));
    xhr.onload = () => {
      if (xhr.status === 401 && state.session?.required) location.replace(location.pathname);
      let d;
      try {
        d = JSON.parse(xhr.responseText);
      } catch {
        return reject(Error("服务未返回有效结果"));
      }
      if (xhr.status >= 200 && xhr.status < 300) resolve(d);
      else
        reject(Error(typeof d.detail === "string" ? d.detail : "上传配置无效"));
    };
    xhr.send(form);
  });
}
async function uploadParts(options) {
  const file = state.file;
  const fingerprint = JSON.stringify([file.name, file.size, file.lastModified, options]);
  if (!state.uploadSession || state.uploadSession.fingerprint !== fingerprint) {
    state.uploadSession = {fingerprint, request_id: crypto.randomUUID().replaceAll('-', ''), id: null};
  }
  const session = state.uploadSession;
  let uploadInfo = session.id ? await api(`/api/uploads/${session.id}`) : await api('/api/uploads', {
    method: 'POST', body: JSON.stringify({filename: file.name, size: file.size, options, request_id: session.request_id}),
  });
  session.id = uploadInfo.upload_id;
  if (uploadInfo.state === 'completed') return api(`/api/tasks/${uploadInfo.task_id}`);
  if (uploadInfo.state !== 'receiving') throw Error(`上传 ${session.id} 的完成结果待检查，请勿重新提交。`);
  try {
    const present = new Set(uploadInfo.parts_present);
    let received = uploadInfo.received_bytes;
    for (let part = 0; part * uploadInfo.part_size < file.size; part++) {
      if (present.has(part)) continue;
      const start = part * uploadInfo.part_size;
      await api(`/api/uploads/${session.id}/parts/${part}`, {method: 'POST',
        headers: {'Content-Type': 'application/octet-stream'}, body: file.slice(start, start + uploadInfo.part_size)});
      received += Math.min(uploadInfo.part_size, file.size - start);
      $('#upload-progress').value = Math.min(100, received / file.size * 100);
    }
    $('#start-transcription').textContent = '正在合并并校验…';
    return await api(`/api/uploads/${session.id}/finish`, {method: 'POST'});
  } catch (error) {
    throw Error(`${error.message}；上传标识 ${session.id}。保留当前页面和文件，再次点击可检查已接收结果。`);
  }
}
function editSegment(id) {
  const s = state.task.segments.find((s) => s.id === id);
  state.edit = {
    segment: s,
    revision: state.task.revision,
    task: state.task.id,
  };
  $("#edit-speaker").innerHTML = state.task.speakers
    .map(
      (p) =>
        `<option value="${esc(p.id)}">${esc(p.name || p.id)} · ${esc(p.role)}</option>`,
    )
    .join("");
  $("#edit-speaker").value = s.speaker_id;
  $("#edit-start").value = s.start;
  $("#edit-end").value = s.end;
  $("#edit-text").value = s.text;
  $("#edit-original").textContent = s.original_text;
  $("#edit-reviewed").checked = s.reviewed;
  $("#edit-error").textContent = "";
  openDialog("#edit-dialog");
}
function editRole(id) {
  const p = state.task.speakers.find((p) => p.id === id);
  state.role = {
    speaker: p,
    revision: state.task.revision,
    task: state.task.id,
  };
  $("#role-title").textContent = `核对人物 · ${p.id}`;
  $("#role-name").value = p.name;
  $("#role-value").value = p.role;
  $("#role-status").value = p.role_status;
  $("#role-note").value = p.evidence_note;
  $("#role-evidence").innerHTML = p.evidence_segment_ids
    .map((id) => {
      const s = state.task.segments.find((s) => s.id === id);
      return s ? `<p>线索 ${clock(s.start)} · ${esc(s.original_text)}</p>` : "";
    })
    .join("");
  $("#role-error").textContent = "";
  openDialog("#role-dialog");
}
function exportFile(kind) {
  if (state.task?.state === "completed") {
    const a = document.createElement("a");
    a.href = `/api/tasks/${state.task.id}/export/${kind}`;
    a.download = "";
    a.click();
  }
}
async function refreshPreview(id) {
  clearTimeout(state.previewPoll);
  if (state.task?.id !== id) return;
  try {
    const d = await api(`/api/tasks/${id}/preview-status`);
    if (state.task?.id !== id) return;
    if (d.status === "ready") {
      $("#audio-player").pause();
      $("#audio-player").hidden = true;
      $("#source-frame").hidden = true;
      $("#video-player").src = `/api/tasks/${id}/media/preview`;
      $("#video-player").hidden = false;
      $("#make-preview").disabled = false;
      $("#preview-hint").textContent = "已生成通用回放，点击发言时间定位视频";
      return;
    }
    if (d.status === "failed") {
      toast("视频回放转换失败，仍可使用原始音轨核听", true);
      $("#make-preview").disabled = false;
      return;
    }
    state.previewPoll = setTimeout(() => refreshPreview(id), 2000);
  } catch (e) {
    toast(e.message, true);
    $("#make-preview").disabled = false;
  }
}
async function init() {
  try {
    const sessionResponse = await fetch('/api/auth/session', {cache:'no-store'});
    // Keep the existing loopback-only v0.1 backend usable during an application upgrade.
    state.session = sessionResponse.status === 404 ? {required:false,authenticated:true} : await api('/api/auth/session');
    if (state.session.required) {
      $('#transport-note').hidden = !state.session.campus_http;
      $('#workspace-label').textContent = '校园个人空间';
      $('#storage-note').textContent = '文件在服务器处理，仅本人可见';
      $('#processing-note').textContent = '在校园服务器处理，保留未经润色的识别原文';
      $('#local-engine-option').textContent = '服务器语音识别 · 在校园服务器处理';
      $('#guide-boundary').textContent = '校园测试版：使用管理员分配的个人凭证登录，任务仅本人可见；退出后任务继续，重新登录可查看。文件在服务器处理。角色、中文庭审与重叠识别尚需真实素材验收；未连接公网法律站。';
      if (state.session.mode === 'platform') {
        $('#guide-boundary').textContent = '使用平台账号，任务仅本人可见；视频分块经云端传输到校内服务器转写。连接失败后保留当前页面和原文件，可检查已接收分块后继续。角色、中文庭审及多人重叠效果仍需核听验收。';
        $('#logout-button').textContent = '退出平台账号';
        $('#cancel-pending-uploads').hidden = false;
      }
      if (!state.session.authenticated) { openDialog('#login-dialog'); return; }
      $('#logout-button').hidden = false;
      $('#workspace-label').textContent = state.session.user.label;
    }
    state.health = await api("/api/health");
    const cap = state.health.capabilities;
    $("#role-agent").disabled = !cap.role_agent;
    $("#environment").textContent = cap.local_asr
      ? `${state.session?.required ? '服务器' : '本地'} ASR 已安装`
      : "需要配置识别环境";
    $("#role-values").innerHTML = state.health.roles
      .map((r) => `<option value="${esc(r)}"></option>`)
      .join("");
  } catch (e) {
    $("#environment").textContent = "服务连接失败";
    toast(e.message, true);
  }
  await loadTasks();
  const id = location.hash.slice(1);
  if (/^[a-f0-9]{32}$/.test(id)) await openTask(id);
}
$('#login-dialog').addEventListener('cancel', (event) => event.preventDefault());
$('#login-form').onsubmit = async (event) => {
  event.preventDefault();
  const button = $('#login-form button'); button.disabled = true;
  $('#login-error').textContent = '';
  try {
    await api('/api/auth/login', { method: 'POST', body: JSON.stringify({ credential: $('#access-credential').value.trim() }) });
    $('#access-credential').value = '';
    location.replace(location.pathname);
  } catch (error) { $('#login-error').textContent = error.message; }
  finally { button.disabled = false; }
};
$('#logout-button').onclick = async () => {
  if (state.uploading) return toast('请等待上传结束后退出');
  try { await api('/api/auth/logout', { method: 'POST' }); clearMedia(); location.replace('/'); }
  catch (error) { toast(error.message, true); }
};
$('#cancel-pending-uploads').onclick = async () => {
  if (state.uploading) return toast('请等待当前上传结束');
  try {
    const uploads = await api('/api/uploads');
    for (const item of uploads.filter(item => ['receiving', 'needs_review'].includes(item.state))) {
      await api(`/api/uploads/${item.upload_id}/abort`, {method: 'POST'});
    }
    state.uploadSession = null;
    toast('已清理本人尚未合并的上传，转写任务保持不变');
  } catch (error) { toast(error.message, true); }
};
$("#choose-file").onclick = $("#new-task").onclick = () =>
  $("#file-input").click();
$("#file-input").onchange = (e) => {
  setFile(e.target.files[0]);
  e.target.value = "";
};
$("#open-demo").onclick = async () => {
  try {
    const t = await api("/api/tasks/demo", { method: "POST" });
    await openTask(t.id);
  } catch (e) {
    toast(e.message, true);
  }
};
$("#home-button").onclick = () => {
  state.requestGeneration++;
  stopPolling();
  clearMedia();
  state.task = null;
  $("#task-view").hidden = true;
  $("#empty-view").hidden = false;
  history.replaceState(null, "", location.pathname);
  closeSidebar();
  loadTasks();
};
$("#guide-button").onclick = () => {
  closeSidebar();
  openDialog("#guide-dialog");
};
$("#mobile-menu").onclick = () => {
  const open = $("#sidebar").classList.toggle("open");
  $("#mobile-menu").setAttribute("aria-expanded", String(open));
};
$("#refresh-tasks").onclick = loadTasks;
document.addEventListener("click", (e) => {
  const t = e.target.closest(
    "[data-task],[data-edit],[data-play],[data-role],[data-filter],.close-dialog",
  );
  if (t) {
    if (t.dataset.task) openTask(t.dataset.task);
    else if (t.dataset.edit) editSegment(t.dataset.edit);
    else if (t.dataset.play) {
      const s = state.task.segments.find((s) => s.id === t.dataset.play);
      playAt(s.start, s.end);
    } else if (t.dataset.role) editRole(t.dataset.role);
    else if (t.dataset.filter) {
      state.filter = t.dataset.filter;
      renderSegments();
    } else if (t.classList.contains("close-dialog"))
      closeDialog(t.closest("dialog"));
  }
  if (window.innerWidth <= 680 && !e.target.closest("#sidebar,#mobile-menu"))
    closeSidebar();
});
$("#drop-zone").addEventListener("dragover", (e) => {
  e.preventDefault();
  $("#drop-zone").classList.add("dragging");
});
$("#drop-zone").addEventListener("dragleave", () =>
  $("#drop-zone").classList.remove("dragging"),
);
$("#drop-zone").addEventListener("drop", (e) => {
  e.preventDefault();
  $("#drop-zone").classList.remove("dragging");
  if (e.dataTransfer.files.length !== 1) {
    toast("请一次导入一个视频文件", true);
    return;
  }
  setFile(e.dataTransfer.files[0]);
});
$("#role-agent").onchange = () => {
  $("#visual-evidence").disabled = !$("#role-agent").checked;
  if (!$("#role-agent").checked) $("#visual-evidence").checked = false;
};
for (const id of ["engine", "diarize", "separate"])
  $("#" + id).onchange = updateSetup;
$("#upload-form").onsubmit = async (e) => {
  e.preventDefault();
  state.uploading = true;
  $("#start-transcription").disabled = true;
  $("#start-transcription").textContent = "正在上传…";
  $("#upload-progress").hidden = false;
  $("#upload-error").textContent = "";
  try {
    const t = await upload();
    state.uploading = false;
    $("#upload-dialog").close();
    await openTask(t.id);
  } catch (err) {
    $("#upload-error").textContent = err.message;
  } finally {
    state.uploading = false;
    $("#start-transcription").textContent = "开始转写 ↗";
    updateSetup();
  }
};
$("#search").oninput = (e) => {
  state.search = e.target.value.trim();
  renderSegments();
};
$("#speaker-filter").onchange = (e) => {
  state.speaker = e.target.value;
  renderSegments();
};
$("#edit-listen").onclick = () =>
  playAt(Number($("#edit-start").value), Number($("#edit-end").value));
$("#edit-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    const t = await api(
      `/api/tasks/${state.edit.task}/segments/${state.edit.segment.id}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          revision: state.edit.revision,
          text: $("#edit-text").value,
          speaker_id: $("#edit-speaker").value,
          start: Number($("#edit-start").value),
          end: Number($("#edit-end").value),
          reviewed: $("#edit-reviewed").checked,
        }),
      },
    );
    state.task = t;
    renderTask();
    $("#edit-dialog").close();
    toast("已保存校对，原始识别文本仍保留");
  } catch (err) {
    $("#edit-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
};
$("#role-form").onsubmit = async (e) => {
  e.preventDefault();
  const button = e.submitter;
  button.disabled = true;
  try {
    const t = await api(
      `/api/tasks/${state.role.task}/speakers/${encodeURIComponent(state.role.speaker.id)}`,
      {
        method: "PATCH",
        body: JSON.stringify({
          revision: state.role.revision,
          name: $("#role-name").value,
          role: $("#role-value").value,
          role_status: $("#role-status").value,
          evidence_note: $("#role-note").value,
          evidence_segment_ids: state.role.speaker.evidence_segment_ids,
        }),
      },
    );
    state.task = t;
    renderTask();
    $("#role-dialog").close();
    toast("已保存人物与身份依据");
  } catch (err) {
    $("#role-error").textContent = err.message;
  } finally {
    button.disabled = false;
  }
};
$("#add-speaker").onclick = async () => {
  try {
    state.task = await api(`/api/tasks/${state.task.id}/speakers`, {
      method: "POST",
      body: JSON.stringify({ revision: state.task.revision }),
    });
    renderTask();
    editRole(state.task.speakers.at(-1).id);
  } catch (e) {
    toast(e.message, true);
  }
};
$("#export-original").onclick = () => exportFile("original");
$("#export-asr").onclick = () =>
  exportFile(state.task.options.engine === "tencent" ? "cloud-raw" : "asr-raw");
$("#export-md").onclick = () => exportFile("markdown");
$("#export-json").onclick = () => exportFile("json");
$("#export-audit").onclick = () => exportFile("audit");
$("#cancel-task").onclick = async () => {
  try {
    await api(`/api/tasks/${state.task.id}/cancel`, { method: "POST" });
    toast("已请求停止；当前模型计算块结束后生效");
  } catch (e) {
    toast(e.message, true);
  }
};
$("#retry-task").onclick = async () => {
  try {
    const t = await api(`/api/tasks/${state.task.id}/retry`, {
      method: "POST",
    });
    await openTask(t.id);
  } catch (e) {
    toast(e.message, true);
  }
};
$("#make-preview").onclick = async () => {
  const id = state.task.id;
  $("#make-preview").disabled = true;
  try {
    await api(`/api/tasks/${id}/preview`, { method: "POST" });
    $("#preview-hint").textContent =
      "正在生成浏览器可播放的视频副本，原视频保留";
    refreshPreview(id);
  } catch (e) {
    toast(e.message, true);
    $("#make-preview").disabled = false;
  }
};
$("#show-frame").onclick = () => {
  const p = sourcePlayer(),
    seconds = Math.max(
      0,
      Math.min(p.currentTime || 0, state.task.media.duration - 0.1),
    );
  $("#source-frame").src =
    `/api/tasks/${state.task.id}/frame?seconds=${seconds}`;
  $("#source-frame").hidden = false;
  $("#preview-hint").textContent =
    `画面时刻 ${clock(seconds, true)} · 用于人工核对身份线索`;
};
$("#source-frame").onerror = () => {
  $("#source-frame").hidden = true;
  toast("该时刻画面提取失败", true);
};
for (const player of [$("#audio-player"), $("#video-player")])
  player.ontimeupdate = () => {
    if (state.playbackEnd !== null && player.currentTime >= state.playbackEnd) {
      player.pause();
      state.playbackEnd = null;
    }
    $$(".segment").forEach((s) =>
      s.classList.toggle(
        "active",
        player.currentTime >= Number(s.dataset.start) &&
          player.currentTime < Number(s.dataset.end),
      ),
    );
  };
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeSidebar();
  if (
    e.key === "/" &&
    !e.ctrlKey &&
    !e.metaKey &&
    !document.querySelector("dialog[open]") &&
    !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName) &&
    state.task
  ) {
    e.preventDefault();
    $("#search").focus();
  }
});
for (const dialog of $$("dialog"))
  dialog.addEventListener("cancel", (e) => {
    if (state.uploading) {
      e.preventDefault();
      toast("正在上传，请等待上传结束");
    }
  });
init();
