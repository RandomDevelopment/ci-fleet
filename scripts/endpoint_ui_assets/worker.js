"use strict";

const interval = 15000;
const minimumInterval = 5000;
const maxInterval = 120000;
const maxBytes = 1024 * 1024;
const pauseMessage = "Refresh paused while you use this view. Displayed measurements are from the last update.";
const refreshStatus = document.getElementById("refresh-status");
let observations = document.getElementById("observations");
let delay = interval;
let nextRefresh = performance.now() + interval;
let timer;
let request;

function report(message) {
  if (refreshStatus.textContent !== message) {
    refreshStatus.textContent = message;
  }
}

function schedule() {
  clearTimeout(timer);
  if (document.hidden || request) return;
  const remaining = Math.max(0, nextRefresh - performance.now());
  if (!remaining && observations.contains(document.activeElement)) {
    report(pauseMessage);
    return;
  }
  timer = setTimeout(refresh, remaining);
}

async function readPage(response) {
  if (![200, 409].includes(response.status) ||
      !response.headers.get("content-type")?.startsWith("text/html;") ||
      Number(response.headers.get("content-length")) > maxBytes) {
    throw new Error("Invalid refresh response");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8", { fatal: true });
  let html = "";
  let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > maxBytes) throw new Error("Refresh response too large");
      html += decoder.decode(value, { stream: true });
    }
    return new DOMParser().parseFromString(html + decoder.decode(), "text/html");
  } finally {
    await reader.cancel();
  }
}

async function refresh() {
  if (document.hidden) return;
  if (observations.contains(document.activeElement)) {
    schedule();
    return;
  }

  const started = performance.now();
  request = new AbortController();
  const timeout = setTimeout(() => request.abort(), 8000);
  let retryAfterFocus = false;
  try {
    const response = await fetch(location.pathname + location.search, {
      cache: "no-store",
      mode: "same-origin",
      redirect: "error",
      signal: request.signal,
    });
    const updated = (await readPage(response)).getElementById("observations");
    if (!updated) throw new Error("Worker view missing");
    const available = updated.dataset.available === "true";
    if (!document.hidden) {
      if (observations.contains(document.activeElement)) {
        retryAfterFocus = available;
        report(pauseMessage);
      } else {
        observations.replaceWith(updated);
        observations = updated;
        report("");
      }
    }
    delay = available ? interval : Math.min(delay * 2, maxInterval);
  } catch {
    if (!document.hidden) {
      report("Refresh unavailable. Any displayed measurements are from the last loaded page.");
      delay = Math.min(delay * 2, maxInterval);
    }
  } finally {
    clearTimeout(timeout);
    request = undefined;
    nextRefresh = started + (retryAfterFocus ? minimumInterval : delay);
    schedule();
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) request?.abort();
  schedule();
});
document.addEventListener("focusout", () => queueMicrotask(schedule));
schedule();
