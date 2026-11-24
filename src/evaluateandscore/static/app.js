"use strict";

const byId = (id) => document.getElementById(id);
const draftKey = "listening-survey-v1";
let setup, round;
let questionIndex = 0, sampleIndex = 0;
let ratings = {};
let submitting = false;

async function api(path, data) {
  const options = data === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(data),
  };
  let response;
  try {
    response = await fetch(path, { ...options, cache: "no-store" });
  } catch {
    throw new Error("无法连接评分服务，请检查网络连接后重试。");
  }
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.error || "请求失败，请重试。");
    error.status = response.status;
    throw error;
  }
  return value;
}

function message(text = "") {
  byId("notice").textContent = text;
  byId("notice").hidden = !text;
}

function show(id) {
  for (const section of ["welcome", "survey", "finish"]) byId(section).hidden = section !== id;
}

function persist() {
  try {
    sessionStorage.setItem(draftKey, JSON.stringify({ id: round.id, ratings, questionIndex, sampleIndex }));
  } catch { /* Scoring also works when tab storage is unavailable. */ }
}

function clearDraft() {
  try { sessionStorage.removeItem(draftKey); } catch { /* Storage is optional. */ }
}

function readDraft() {
  try { return JSON.parse(sessionStorage.getItem(draftKey)); } catch { return null; }
}

async function loadSetup() {
  setup = await api("/api/setup");
  byId("round-info").textContent = setup.samples
    ? "每轮 " + setup.questions + " 题　·　每题 " + setup.samples + " 段录音"
    : "暂无待评录音，请在 datasets/test 添加音频后刷新页面。";
  byId("reference-info").textContent = setup.has_reference
    ? "可随时播放真人参考音作对照，再独立评价每段匿名录音。"
    : "当前未设置参考音，可根据日常听感独立评价每段匿名录音。";
  byId("criteria").replaceChildren(...setup.dimensions.map(({ label, hint }) => {
    const li = document.createElement("li");
    const title = document.createElement("strong");
    const detail = document.createElement("span");
    title.textContent = label;
    detail.textContent = hint;
    li.append(title, detail);
    return li;
  }));
  byId("start").disabled = !setup.samples;
}

function current() {
  return round.questions[questionIndex][sampleIndex];
}

function complete(sample) {
  return setup.dimensions.every((d) => Number.isInteger(ratings[sample.id]?.[d.id]));
}

function selectPage(q, s, focus = false) {
  questionIndex = q;
  sampleIndex = s;
  persist();
  render();
  if (focus) byId("question-title").focus();
}

function updateProgress() {
  const all = round.questions.flat();
  const done = all.filter(complete).length;
  byId("progress").max = all.length;
  byId("progress").value = done;
  byId("progress-count").textContent = "已评 " + done + " / " + all.length + " 段";
  [...byId("sample-tabs").children].forEach((button, index) => {
    const sample = round.questions[questionIndex][index];
    button.textContent = "样本 " + (index + 1) + (complete(sample) ? " ✓" : "");
  });
}

function ratingRow(dimension) {
  const row = document.createElement("div");
  row.className = "rating-row";
  const title = document.createElement("strong");
  const hint = document.createElement("small");
  const caption = document.createElement("div");
  title.id = "label-" + dimension.id;
  title.textContent = dimension.label;
  hint.textContent = dimension.hint;
  caption.append(title, hint);
  const scores = document.createElement("div");
  scores.className = "scores";
  scores.setAttribute("role", "radiogroup");
  scores.setAttribute("aria-labelledby", title.id);
  const id = current().id;
  for (let score = 1; score <= 5; score++) {
    const label = document.createElement("label");
    const input = document.createElement("input");
    input.type = "radio";
    input.name = dimension.id;
    input.value = score;
    input.checked = ratings[id]?.[dimension.id] === score;
    input.setAttribute("aria-label", score + " 分");
    input.onchange = () => {
      (ratings[id] ??= {})[dimension.id] = score;
      byId("validation").textContent = "";
      persist();
      updateProgress();
    };
    const glyph = document.createElement("span");
    glyph.textContent = score;
    label.append(input, glyph);
    scores.append(label);
  }
  row.append(caption, scores);
  return row;
}

function render() {
  show("survey");
  byId("validation").textContent = "";
  const samples = round.questions[questionIndex];
  byId("question-title").textContent = "第 " + (questionIndex + 1) + " / " + round.questions.length + " 题";
  byId("reference-panel").hidden = !round.reference_url;
  if (round.reference_url && byId("reference").getAttribute("src") !== round.reference_url) {
    byId("reference").src = round.reference_url;
  }
  byId("reference").pause();
  byId("sample-tabs").replaceChildren(...samples.map((sample, index) => {
    const button = document.createElement("button");
    button.setAttribute("aria-current", String(index === sampleIndex));
    button.onclick = () => selectPage(questionIndex, index);
    return button;
  }));
  byId("clip-name").textContent = "样本 " + (sampleIndex + 1);
  byId("audio-error").hidden = true;
  byId("clip").src = current().audio_url;
  byId("rating-rows").replaceChildren(...setup.dimensions.map(ratingRow));
  byId("sample-feedback").value = ratings[current().id]?.feedback ?? "";
  byId("back").disabled = !questionIndex && !sampleIndex;
  const lastSample = sampleIndex === samples.length - 1;
  const lastQuestion = questionIndex === round.questions.length - 1;
  byId("next").textContent = lastSample ? (lastQuestion ? "提交本轮评分" : "下一题") : "下一个样本";
  updateProgress();
}

function finished(receipt) {
  clearDraft();
  for (const player of [byId("clip"), byId("reference")]) {
    player.pause();
    player.removeAttribute("src");
    player.load();
  }
  message();
  show("finish");
  byId("finish-message").textContent = "本轮已于 " + new Date(receipt.submitted_at).toLocaleString("zh-CN") + " 提交。";
  byId("finish-title").focus();
}

async function start() {
  byId("start").disabled = byId("again").disabled = true;
  message();
  try {
    round = await api("/api/rounds", {});
    ratings = {};
    selectPage(0, 0, true);
  } catch (error) {
    message(error.message);
  } finally {
    byId("start").disabled = !setup.samples;
    byId("again").disabled = false;
  }
}

function requireScores() {
  if (complete(current())) return true;
  byId("validation").textContent = "请为本段录音的五个维度都选择分数。";
  const missing = setup.dimensions.find((d) => !ratings[current().id]?.[d.id]);
  document.querySelector('input[name="' + missing.id + '"]').focus();
  return false;
}

async function next() {
  if (submitting || !requireScores()) return;
  if (sampleIndex + 1 < round.questions[questionIndex].length) {
    selectPage(questionIndex, sampleIndex + 1, true);
  } else if (questionIndex + 1 < round.questions.length) {
    selectPage(questionIndex + 1, 0, true);
  } else {
    for (const [q, samples] of round.questions.entries()) {
      const missing = samples.findIndex((s) => !complete(s));
      if (missing >= 0) {
        selectPage(q, missing);
        requireScores();
        return;
      }
    }
    submitting = true;
    byId("survey").inert = true;
    byId("next").textContent = "正在提交…";
    message();
    try {
      finished(await api("/api/rounds/" + round.id + "/submit", { ratings }));
    } catch (error) {
      message(error.message);
      if (error.status === 404) {
        clearDraft();
        byId("reference").pause();
        byId("clip").pause();
        show("welcome");
      }
    } finally {
      byId("next").textContent = "提交本轮评分";
      byId("survey").inert = false;
      submitting = false;
    }
  }
}

byId("clip").onplay = () => byId("reference").pause();
byId("reference").onplay = () => byId("clip").pause();
byId("clip").onerror = () => { byId("audio-error").hidden = false; };
byId("sample-feedback").oninput = (event) => {
  (ratings[current().id] ??= {}).feedback = event.target.value;
  persist();
};
byId("next").onclick = next;
byId("back").onclick = () => {
  if (sampleIndex > 0) selectPage(questionIndex, sampleIndex - 1, true);
  else if (questionIndex > 0) selectPage(questionIndex - 1, round.questions[questionIndex - 1].length - 1, true);
};
byId("start").onclick = byId("again").onclick = start;

async function init() {
  try {
    await loadSetup();
    const draft = readDraft();
    if (draft?.id) {
      const state = await api("/api/rounds/" + draft.id);
      if (state.submitted_at) return finished(state);
      round = state;
      ratings = draft.ratings || {};
      if (round.questions[draft.questionIndex]?.[draft.sampleIndex]) {
        selectPage(draft.questionIndex, draft.sampleIndex);
      } else {
        selectPage(0, 0);
      }
    }
  } catch (error) {
    if (error.status === 404) clearDraft();
    message(error.message);
  }
}
init();
