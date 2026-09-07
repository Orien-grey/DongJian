// Local-only Edge CDP acceptance harness. It uses the existing project-local
// Node runtime and installed Edge; it does not install Playwright or any
// browser dependency and is not part of the portable runtime.

import { spawn } from "node:child_process";
import { mkdirSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const PYTHON = resolve(ROOT, "runtime", "venv", "Scripts", "python.exe");
const SERVER_SCRIPT = resolve(ROOT, "scripts", "browser_acceptance_server.py");
const EDGE = "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe";
const TEMP_ROOT = resolve(ROOT, "cache", "temp", "browser-acceptance");
const RUN_ID = `${Date.now()}-${process.pid}`;
const EDGE_PROFILE = resolve(TEMP_ROOT, `edge-profile-${RUN_ID}`);
const DEBUG_PORT = 19245;

const sleep = (milliseconds) => new Promise((resolvePromise) => setTimeout(resolvePromise, milliseconds));

class CdpPage {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.socket = null;
  }

  async connect() {
    this.socket = new WebSocket(this.url);
    await new Promise((resolvePromise, reject) => {
      this.socket.addEventListener("open", resolvePromise, { once: true });
      this.socket.addEventListener("error", (event) => reject(new Error(`CDP WebSocket error: ${event.message || "unknown"}`)), { once: true });
    });
    this.socket.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (!message.id) return;
      const waiter = this.pending.get(message.id);
      if (!waiter) return;
      this.pending.delete(message.id);
      if (message.error) waiter.reject(new Error(`${message.error.code}: ${message.error.message}`));
      else waiter.resolve(message.result);
    });
    await this.send("Page.enable");
    await this.send("Runtime.enable");
  }

  send(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolvePromise, reject) => {
      this.pending.set(id, { resolve: resolvePromise, reject });
      this.socket.send(JSON.stringify({ id, method, params }));
    });
  }

  async evaluate(expression) {
    const result = await this.send("Runtime.evaluate", {
      expression,
      awaitPromise: true,
      returnByValue: true,
      userGesture: true,
    });
    if (result.exceptionDetails) {
      throw new Error(result.exceptionDetails.text || "browser expression failed");
    }
    return result.result?.value;
  }

  async close() {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) this.socket.close();
  }
}

async function waitForHttpJson(url, predicate, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  let lastError = "";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      const value = await response.json();
      if (predicate(value)) return value;
    } catch (error) {
      lastError = String(error);
    }
    await sleep(200);
  }
  throw new Error(`timed out waiting for ${url}${lastError ? ` (${lastError})` : ""}`);
}

async function waitFor(page, expression, label, timeout = 20_000) {
  const deadline = Date.now() + timeout;
  let lastValue;
  while (Date.now() < deadline) {
    try {
      lastValue = await page.evaluate(expression);
      if (lastValue) return lastValue;
    } catch (error) {
      lastValue = String(error);
    }
    await sleep(250);
  }
  throw new Error(`timed out waiting for ${label}; last value: ${JSON.stringify(lastValue)}`);
}

async function clickButton(page, text, index = 0) {
  const expression = `(() => {
    const wanted = ${JSON.stringify(text)};
    const nodes = [...document.querySelectorAll("button")].filter((node) => (node.innerText || "").includes(wanted) && !node.disabled);
    const node = nodes[${index}];
    if (!node) return false;
    node.click();
    return true;
  })()`;
  return waitFor(page, expression, `button ${text}`);
}

async function clickSelector(page, selector, text = "element") {
  const expression = `(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node || node.disabled) return false;
    node.click();
    return true;
  })()`;
  return waitFor(page, expression, text);
}

async function clickContaining(page, selector, text) {
  const expression = `(() => {
    const wanted = ${JSON.stringify(text)};
    const node = [...document.querySelectorAll(${JSON.stringify(selector)})].find((item) => (item.innerText || "").includes(wanted));
    if (!node) return false;
    node.click();
    return true;
  })()`;
  return waitFor(page, expression, `${selector} containing ${text}`);
}

async function fill(page, selector, value) {
  const expression = `(() => {
    const node = document.querySelector(${JSON.stringify(selector)});
    if (!node) return false;
    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")?.set || Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")?.set;
    if (!setter) return false;
    setter.call(node, ${JSON.stringify(value)});
    node.dispatchEvent(new Event("input", { bubbles: true }));
    node.dispatchEvent(new Event("change", { bubbles: true }));
    return node.value === ${JSON.stringify(value)};
  })()`;
  return waitFor(page, expression, `fill ${selector}`);
}

async function bodyText(page) {
  return page.evaluate("document.body?.innerText || ''");
}

async function assertBody(page, text, label = text) {
  await waitFor(page, `document.body?.innerText.includes(${JSON.stringify(text)})`, label);
}

async function assertNoLoading(page, label) {
  await sleep(700);
  const count = await page.evaluate("document.querySelectorAll('.loading-box, .settings-load-state').length");
  if (count !== 0) throw new Error(`${label}: persistent loading element count=${count}`);
}

async function main() {
  if (!statSync(EDGE).isFile()) throw new Error("Edge executable is not readable");
  mkdirSync(TEMP_ROOT, { recursive: true });
  const checks = [];
  let server = null;
  let browser = null;
  let page = null;
  try {
    server = spawn(PYTHON, [SERVER_SCRIPT, "--port", "0"], {
      cwd: ROOT,
      env: {
        ...process.env,
        PYTHONNOUSERSITE: "1",
        PYTHONPATH: resolve(ROOT, "src"),
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    let serverOutput = "";
    server.stdout.on("data", (chunk) => { serverOutput += chunk.toString(); });
    server.stderr.on("data", (chunk) => { serverOutput += chunk.toString(); });
    const serverPort = await new Promise((resolvePromise, reject) => {
      const deadline = setTimeout(() => reject(new Error(`server did not publish a port: ${serverOutput}`)), 60_000);
      const onData = () => {
        const match = serverOutput.match(/PORT=(\d+)/);
        if (!match) return;
        clearTimeout(deadline);
        resolvePromise(Number(match[1]));
      };
      server.stdout.on("data", onData);
      server.on("exit", (code) => reject(new Error(`fixture server exited ${code}: ${serverOutput}`)));
    });
    const appUrl = `http://127.0.0.1:${serverPort}/`;
    browser = spawn(EDGE, [
      "--headless",
      "--disable-gpu",
      "--disable-gpu-compositing",
      "--in-process-gpu",
      "--disable-background-networking",
      "--disable-component-update",
      "--disable-domain-reliability",
      "--no-first-run",
      "--no-default-browser-check",
      "--remote-allow-origins=*",
      `--remote-debugging-port=${DEBUG_PORT}`,
      `--user-data-dir=${EDGE_PROFILE}`,
      appUrl,
    ], { cwd: ROOT, stdio: "ignore" });
    const targets = await waitForHttpJson(`http://127.0.0.1:${DEBUG_PORT}/json/list`, (items) => Array.isArray(items) && items.some((item) => item.type === "page" && item.webSocketDebuggerUrl));
    const target = targets.find((item) => item.type === "page" && item.webSocketDebuggerUrl);
    page = new CdpPage(target.webSocketDebuggerUrl);
    await page.connect();
    await assertBody(page, "项目资料", "initial application load");
    checks.push("application loaded in Edge");

    const sticky = await page.evaluate("getComputedStyle(document.querySelector('.topbar')).position === 'sticky'");
    if (!sticky) throw new Error("topbar is not sticky");
    checks.push("sticky topbar");

    await clickButton(page, "处理新目录");
    await waitFor(page, "document.querySelector('#source-path') !== null", "process dialog");
    // The fixture server publishes the dynamic path on stdout; the browser
    // harness gets it from the synthetic, deterministic path convention.
    const dynamicPath = resolve(TEMP_ROOT, "dynamic-input");
    await fill(page, "#source-path", dynamicPath);
    await clickButton(page, "开始处理");
    await waitFor(page, "fetch('/api/v1/tasks?limit=20').then((response) => response.json()).then((value) => value.items.some((item) => item.taskType === 'process' && ['queued','running'].includes(item.status)))", "local process task running", 15_000);
    checks.push("catalog processing task observed");

    await clickButton(page, "数据目录");
    await waitFor(page, "document.body.innerText.includes('dynamic-0.txt')", "incremental catalog visibility", 20_000);
    await clickContaining(page, ".file-list-item", "dynamic-0.txt");
    await waitFor(page, "document.body.innerText.includes('dynamic-0.txt') && document.querySelector('.file-detail-section') !== null", "open first READY_LOCAL file while processing");
    checks.push("first local file opened while next files were processing");
    await assertNoLoading(page, "dynamic file viewer");

    await clickSelector(page, "button.back-button", "back to catalog");
    await waitFor(page, "document.body.innerText.includes('数据目录') && document.querySelector('.file-list-item') !== null", "catalog after dynamic file");

    await clickContaining(page, ".file-list-item", "two-pages.pdf");
    await waitFor(page, "document.body.innerText.includes('Page 1 / 2')", "PDF page one");
    await clickButton(page, "下一页");
    await waitFor(page, "document.body.innerText.includes('Page 2 / 2') && document.body.innerText.includes('PDF page two')", "PDF next page");
    await clickButton(page, "上一页");
    await waitFor(page, "document.body.innerText.includes('Page 1 / 2')", "PDF previous page");
    await fill(page, "input[aria-label='跳转页码']", "2");
    await waitFor(page, "document.body.innerText.includes('Page 2 / 2')", "PDF jump page");
    await clickButton(page, "原始页面");
    await waitFor(page, "document.querySelectorAll('.source-page-preview img').length > 0", "PDF source page preview");
    await clickButton(page, "整理阅读");
    await fill(page, "input[aria-label='查找当前文件']", "page two");
    await clickButton(page, "查找");
    await waitFor(page, "document.body.innerText.includes('1 / 1') && document.body.innerText.includes('PDF page two')", "PDF file-local search");
    checks.push("PDF prev/next/jump/source/search");
    await clickSelector(page, "button.back-button", "back from PDF");
    await waitFor(page, "document.querySelector('.file-list-item') !== null", "catalog for XLSX");

    await clickContaining(page, ".file-list-item", "book.xlsx");
    await waitFor(page, "document.body.innerText.includes('Sheet A') && document.querySelector('.sheet-tabs') !== null", "XLSX first sheet");
    await clickContaining(page, ".sheet-tabs button", "Sheet B");
    await waitFor(page, "document.body.innerText.includes('South')", "XLSX sheet switch");
    await clickButton(page, "原始矩阵");
    await clickButton(page, "规范化数据");
    checks.push("XLSX sheet switch and raw/normalized reading");
    await clickSelector(page, "button.back-button", "back from XLSX");
    await waitFor(page, "document.querySelector('.file-list-item') !== null", "catalog for DOCX");

    await clickContaining(page, ".file-list-item", "form.docx");
    await waitFor(page, "document.body.innerText.includes('DOCX introduction') && document.body.innerText.includes('原貌阅读')", "DOCX continuous and form view");
    await fill(page, "input[aria-label='查找当前文件']", "DOCX conclusion");
    await clickButton(page, "查找");
    await waitFor(page, "document.querySelectorAll('.locator-highlight').length > 0", "DOCX search locator highlight");
    checks.push("DOCX form table and search locator");
    await clickSelector(page, "button.back-button", "back from DOCX");
    await waitFor(page, "document.querySelector('.file-list-item') !== null", "catalog for TXT");

    await clickContaining(page, ".file-list-item", "notes.txt");
    await waitFor(page, "document.body.innerText.includes('TXT continuous reading')", "TXT continuous reader");
    await fill(page, "input[aria-label='查找当前文件']", "needle");
    await clickButton(page, "查找");
    await waitFor(page, "document.querySelectorAll('.locator-highlight').length > 0 && document.body.innerText.includes('下一个')", "TXT locator and next/previous controls");
    await clickButton(page, "下一个");
    await clickButton(page, "上一个");
    checks.push("TXT search locator and next/previous");
    await clickSelector(page, "button.back-button", "back from TXT");
    await waitFor(page, "document.querySelector('.file-list-item') !== null", "catalog for PNG");

    await clickContaining(page, ".file-list-item", "table.png");
    await waitFor(page, "document.querySelector('.source-image-preview img') !== null && document.body.innerText.includes('原始图片')", "PNG source image");
    const hasCandidate = await waitFor(page, "document.querySelector('.candidate-notice') !== null || document.querySelector('.candidate-label') !== null", "PNG candidate presentation", 20_000);
    if (!hasCandidate) throw new Error("PNG candidate presentation was not rendered");
    checks.push("PNG source and candidate presentation");

    await clickButton(page, "设置");
    await waitFor(page, "document.body.innerText.includes('AI 文件整理') && document.body.innerText.includes('整理所有未整理文件')", "FileInsight settings");
    await clickButton(page, "整理所有未整理文件");
    await assertBody(page, "将向当前配置的 AI 服务发送每个文件的有界整理上下文。", "bulk FileInsight confirmation copy");
    await clickButton(page, "确认整理");
    await waitFor(page, "document.body.innerText.includes('已完成') && document.body.innerText.includes('等待') && document.body.innerText.includes('处理中') && document.body.innerText.includes('失败')", "FileInsight queue status rendering");
    checks.push("FileInsight bulk confirmation and queue counters");
    await clickButton(page, "处理任务");
    await waitFor(page, "document.body.innerText.includes('AI 文件整理') && document.body.innerText.includes('有界上下文')", "FileInsight task card");
    checks.push("FileInsight task progress card");

    await clickButton(page, "报告");
    await waitFor(page, "document.body.innerText.includes('选择资料') && document.body.innerText.includes('生成报告')", "report workflow page");
    const reportText = await bodyText(page);
    for (const forbidden of ["AnalysisRun", "ReportComposer", "Orchestrator"]) {
      if (reportText.includes(forbidden)) throw new Error(`report page exposes internal term ${forbidden}`);
    }
    checks.push("report page uses user workflow terminology only");
    await assertNoLoading(page, "report page");

    const screenshot = await page.send("Page.captureScreenshot", { format: "png" });
    writeFileSync(resolve(TEMP_ROOT, "browser-acceptance.png"), Buffer.from(screenshot.data, "base64"));
    console.log(JSON.stringify({ status: "PASS", checks, screenshot: resolve(TEMP_ROOT, "browser-acceptance.png") }, null, 2));
    return 0;
  } finally {
    if (page) await page.close().catch(() => {});
    if (browser && !browser.killed) browser.kill();
    if (server && !server.killed) server.kill();
  }
}

main().catch((error) => {
  console.error(JSON.stringify({ status: "FAIL", error: String(error?.stack || error) }, null, 2));
  process.exitCode = 1;
});
